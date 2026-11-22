"""Weekly Markdown health report for a notes vault (e.g. Obsidian + Dataview).

Pure module (no FastMCP import), built on `queries.py`: numbers come from the same
aggregates the MCP tools return, so the report and the tools never disagree. The
output is deterministic (no generation timestamp), so regenerating an unchanged week
is a no-op for the vault.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from apple_health_export_mcp import queries

_ISO_WEEK = re.compile(r"(\d{4})-W(\d{2})")


def week_range(d: str) -> tuple[str, str]:
    """Monday–Sunday calendar week containing date `d` ('YYYY-MM-DD')."""
    dt = date.fromisoformat(d)
    monday = dt - timedelta(days=dt.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def resolve_week(
    *, week: str | None = None, week_of: str | None = None, today: date | None = None
) -> tuple[str, str, str]:
    """Return (label 'YYYY-Www', monday, sunday) for the requested week.

    `week` is an ISO week ('2026-W47'), `week_of` any date inside the week. With
    neither, the last completed Monday–Sunday week before `today`.
    """
    if week and week_of:
        raise ValueError("Pass week or week_of, not both.")
    if week:
        bad = f"week must be an ISO week 'YYYY-Www', got {week!r}"
        m = _ISO_WEEK.fullmatch(week)
        if not m:
            raise ValueError(bad)
        try:
            day = date.fromisocalendar(int(m[1]), int(m[2]), 1).isoformat()
        except ValueError:
            raise ValueError(bad) from None
    else:
        day = week_of or ((today or date.today()) - timedelta(days=7)).isoformat()
    start, end = week_range(day)
    year, num, _ = date.fromisoformat(start).isocalendar()
    return f"{year}-W{num:02d}", start, end


_STEPS = "HKQuantityTypeIdentifierStepCount"
_KCAL = "HKQuantityTypeIdentifierActiveEnergyBurned"
_RHR = "HKQuantityTypeIdentifierRestingHeartRate"
_HRV = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
# Fixed names: strftime('%a') follows the process locale, which would make the
# output depend on where the job runs.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _fmt(v: float | None, empty: str) -> str:
    """One decimal, dropped when whole ('2000', '45', '12.5'); `empty` for no data."""
    if v is None:
        return empty
    r = round(v, 1)
    return str(int(r)) if r == int(r) else str(r)


def _total(values) -> float | None:
    """Sum of the values that exist; None (no data) rather than 0 when none do."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _mean(values) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def build(conn: sqlite3.Connection, label: str, start: str, end: str) -> str:
    """Markdown report for the week `start`..`end` (YAML frontmatter + tables).

    Steps and active energy are per-day sums de-duplicated across devices (ADR-0010);
    `steps_daily_avg` averages over the days that have step data, not over 7. Resting
    HR and HRV average the samples. Sleep counts nights with time asleep, attributed
    to the wake-up day. A metric with no data is `null`, never 0.
    """

    def daily(type_: str, agg: str) -> list[queries.QuantityPoint]:
        return queries.quantity(conn, type_, start, end, agg, "day")

    def sample_mean(points: list[queries.QuantityPoint]) -> float | None:
        # Daily avgs weighted by their sample counts = the week's sample mean.
        n = sum(p.n for p in points)
        return sum(p.value * p.n for p in points) / n if n else None

    steps, kcal = daily(_STEPS, "sum"), daily(_KCAL, "sum")
    rhr, hrv = daily(_RHR, "avg"), daily(_HRV, "avg")
    nights = [n for n in queries.sleep(conn, start, end) if n.asleep_min > 0]
    workouts = queries.workouts(conn, start, end)
    # (label for "no data", per-day values) in daily-table column order.
    series = [
        ("steps", {p.period: p.value for p in steps}),
        ("active energy", {p.period: p.value for p in kcal}),
        ("resting heart rate", {p.period: p.value for p in rhr}),
        ("HRV", {p.period: p.value for p in hrv}),
        ("sleep", {n.night: n.asleep_min for n in nights}),
    ]

    fields = {
        "steps_total": _total(p.value for p in steps),
        "steps_daily_avg": _mean(p.value for p in steps),
        "active_kcal_total": _total(p.value for p in kcal),
        "resting_hr_avg": sample_mean(rhr),
        "hrv_avg": sample_mean(hrv),
        "sleep_asleep_avg_min": _mean(n.asleep_min for n in nights),
        "sleep_nights": len(nights),
        "workouts_count": sum(w.n for w in workouts),
        "workout_min_total": _total(w.total_min for w in workouts),
        "workout_km_total": _total(w.total_distance_km for w in workouts),
        "workout_kcal_total": _total(w.total_energy_kcal for w in workouts),
    }
    out = ["---", f"week: {label}", f"start: {start}", f"end: {end}"]
    out += [f"{k}: {_fmt(v, 'null')}" for k, v in fields.items()]
    out += ["---", "", f"# Health week {label}", "", f"{start} to {end}", "", "## Daily", ""]
    out += ["| Day | Steps | Active kcal | Resting HR | HRV (ms) | Asleep (min) |", "|---|---|---|---|---|---|"]
    monday = date.fromisoformat(start)
    for i in range(7):
        d = (monday + timedelta(days=i)).isoformat()
        cells = [_fmt(values.get(d), "–") for _, values in series]
        out.append(f"| {d} {_WEEKDAYS[i]} | " + " | ".join(cells) + " |")

    out += ["", "## Workouts", ""]
    if workouts:
        out.append("| Activity | Sessions | Minutes | Longest (min) | km | kcal | Avg HR |")
        out.append("|---|---|---|---|---|---|---|")
        for w in workouts:
            name = w.activity.removeprefix("HKWorkoutActivityType")
            nums = (w.n, w.total_min, w.max_min, w.total_distance_km, w.total_energy_kcal, w.avg_heart_rate)
            out.append(f"| {name} | " + " | ".join(_fmt(v, "–") for v in nums) + " |")
    else:
        out.append("No workouts this week.")

    missing = [name for name, values in series if not values]
    if missing:
        out += ["", f"No data this week: {', '.join(missing)}."]
    return "\n".join(out) + "\n"


def write(out_dir: Path, label: str, text: str) -> Path:
    """Write `<out_dir>/<label>.md`, skipping the write when the content is unchanged.

    An unchanged rewrite would still bump the mtime and wake up vault sync. The new
    content goes to a temp file first and is renamed over the note, so a vault app
    never reads a half-written file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    note = out_dir / f"{label}.md"
    if note.exists() and note.read_text(encoding="utf-8") == text:
        return note
    tmp = note.with_name(f".{note.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, note)
    return note
