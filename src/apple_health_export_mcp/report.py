"""Weekly Markdown health report for a notes vault (e.g. Obsidian + Dataview).

Pure module (no FastMCP import), built on `queries.py`: numbers come from the same
aggregates the MCP tools return, so the report and the tools never disagree. The
output is deterministic (no generation timestamp), so regenerating an unchanged week
is a no-op for the vault.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from apple_health_export_mcp import queries
from apple_health_export_mcp.queries import week_range

_ISO_WEEK = re.compile(r"(\d{4})-W(\d{2})")


def _label(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def resolve_week(
    *, week: str | None = None, week_of: str | None = None, today: date | None = None
) -> tuple[str, str, str]:
    """Return (label 'YYYY-Www', monday, sunday) for the requested week.

    `week` is an ISO week ('2026-W47'), `week_of` any date inside the week. With
    neither, the last completed Monday–Sunday week before `today`.
    """
    if week and week_of:
        raise ValueError("Pass --week or --week-of, not both.")
    if week:
        m = _ISO_WEEK.fullmatch(week)
        try:
            if not m:
                raise ValueError
            monday = date.fromisocalendar(int(m[1]), int(m[2]), 1)
        except ValueError:
            raise ValueError(f"week must be an ISO week 'YYYY-Www', got {week!r}") from None
        day = monday.isoformat()
    else:
        day = week_of or ((today or date.today()) - timedelta(days=7)).isoformat()
    start, end = week_range(day)
    return _label(date.fromisoformat(start)), start, end


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
    return sum(values) / len(values) if values else None


def build(conn: sqlite3.Connection, label: str, start: str, end: str) -> str:
    """Markdown report for the week `start`..`end` (YAML frontmatter + tables).

    Steps and active energy are per-day sums de-duplicated across devices (ADR-0010);
    `steps_daily_avg` averages over the days that have step data, not over 7. Resting
    HR and HRV average the samples. Sleep counts nights with time asleep, attributed
    to the wake-up day. A metric with no data is `null`, never 0.
    """

    def daily(type_: str, agg: str) -> dict[str, float]:
        return {p.period: p.value for p in queries.quantity(conn, type_, start, end, agg, "day")}

    def overall(type_: str) -> float | None:
        rows = queries.quantity(conn, type_, start, end, "avg", "all")
        return rows[0].value if rows else None

    steps, kcal, rhr, hrv = daily(_STEPS, "sum"), daily(_KCAL, "sum"), daily(_RHR, "avg"), daily(_HRV, "avg")
    asleep = {n.night: n.asleep_min for n in queries.sleep(conn, start, end) if n.asleep_min > 0}
    workouts = queries.workouts(conn, start, end)

    fields = {
        "steps_total": _total(steps.values()),
        "steps_daily_avg": _mean(list(steps.values())),
        "active_kcal_total": _total(kcal.values()),
        "resting_hr_avg": overall(_RHR),
        "hrv_avg": overall(_HRV),
        "sleep_asleep_avg_min": _mean(list(asleep.values())),
        "sleep_nights": len(asleep),
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
        cells = [_fmt(m.get(d), "–") for m in (steps, kcal, rhr, hrv, asleep)]
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

    missing = [
        name
        for name, series in (
            ("steps", steps),
            ("active energy", kcal),
            ("resting heart rate", rhr),
            ("HRV", hrv),
            ("sleep", asleep),
        )
        if not series
    ]
    if missing:
        out += ["", f"No data this week: {', '.join(missing)}."]
    return "\n".join(out) + "\n"
