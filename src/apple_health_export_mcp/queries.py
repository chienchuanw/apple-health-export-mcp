"""Pure SQLite query helpers — no FastMCP dependency, so they're unit-testable.

All return aggregates only (ADR-0008). Dates are local-day strings 'YYYY-MM-DD'
compared against record.local_date (ADR-0009). Durations strip the " +HHMM"
offset before julianday(); start/end share an offset so the diff is correct.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, timedelta


def week_range(d: str) -> tuple[str, str]:
    """Monday–Sunday calendar week containing date `d` ('YYYY-MM-DD')."""
    dt = date.fromisoformat(d)
    monday = dt - timedelta(days=dt.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


@dataclass
class TypeInfo:
    """A metric present in the data, with its row count and date span."""

    type: str
    n: int
    first: str | None
    last: str | None


@dataclass
class SourceInfo:
    """A device/app that wrote a metric, with its row count and date span."""

    source: str | None
    n: int
    first: str | None
    last: str | None


@dataclass
class QuantityPoint:
    """One bucket of an aggregated numeric metric."""

    period: str
    value: float
    n: int


@dataclass
class SleepNight:
    """Sleep stage durations (minutes) for one night, keyed by the wake-up day."""

    night: str
    asleep_min: float
    rem_min: float
    deep_min: float
    core_min: float
    awake_min: float
    in_bed_min: float


@dataclass
class WorkoutSummary:
    """Per-activity workout totals over a range (units normalized, ADR-0011)."""

    activity: str
    n: int
    total_min: float | None
    avg_min: float | None
    max_min: float | None  # longest single workout; far above avg_min = likely left running
    total_distance_km: float | None
    total_energy_kcal: float | None
    avg_heart_rate: float | None  # bpm, weighted by workout duration


@dataclass
class Baseline:
    """A metric's recent mean against the user's own preceding baseline (ADR-0017).

    Means are over days that have data (per-day values as `get_quantity` buckets them,
    or minutes asleep per night for sleep). `z` is delta / baseline_sd.
    """

    type: str
    agg: str
    recent_start: str
    recent_end: str
    recent_mean: float | None
    recent_n: int  # days with data in the recent window
    baseline_start: str
    baseline_end: str
    baseline_mean: float | None
    baseline_sd: float | None  # sample standard deviation of the daily values
    baseline_n: int  # days with data in the baseline window
    delta: float | None  # recent_mean - baseline_mean
    delta_pct: float | None  # delta as % of baseline_mean
    z: float | None  # null with < 7 baseline days or zero spread


@dataclass
class TrainingWeek:
    """Workout volume for one Monday–Sunday week, with its acute:chronic ratio (ADR-0017)."""

    week_start: str
    week_end: str
    sessions: int
    total_min: float
    total_km: float | None  # 0 without workouts; null if workouts lack distance data
    total_kcal: float | None
    acwr: float | None  # total_min / mean of the 4 previous weeks; null if that mean is 0


_BUCKET = {
    "day": "local_date",
    "week": "strftime('%Y-W%W', local_date)",
    "month": "substr(local_date,1,7)",
    "all": "'all'",
}
_AGG = {"sum", "avg", "min", "max", "count"}
# Stages that count as actually asleep (vs InBed/Awake).
_ASLEEP = (
    "HKCategoryValueSleepAnalysisAsleepCore",
    "HKCategoryValueSleepAnalysisAsleepDeep",
    "HKCategoryValueSleepAnalysisAsleepREM",
    "HKCategoryValueSleepAnalysisAsleepUnspecified",
)


def list_types(conn: sqlite3.Connection) -> list[TypeInfo]:
    """Discover which metrics exist in this data (ADR-0004): type, count, date span."""
    rows = conn.execute(
        "SELECT type, COUNT(*) n, MIN(local_date) first, MAX(local_date) last FROM record GROUP BY type ORDER BY n DESC"
    ).fetchall()
    out = [TypeInfo(r["type"], r["n"], r["first"], r["last"]) for r in rows]
    wk = conn.execute("SELECT COUNT(*) n FROM workout").fetchone()["n"]
    if wk:
        out.append(TypeInfo("Workout", wk, None, None))
    return out


def quantity(
    conn: sqlite3.Connection,
    type: str,
    start: str,
    end: str,
    agg: str = "sum",
    bucket: str = "day",
    source: str | None = None,
) -> list[QuantityPoint]:
    """Aggregate a numeric metric over a date range.

    `sum` is de-duplicated: the same activity is logged in parallel by multiple
    devices (Watch + iPhone + 3rd-party), so a naive SUM over-counts (~40% on
    real data). We mirror Apple's "one source per interval" merge offline by
    picking, per local day, the single dominant source (most records that day =
    the device actually in use) and summing only it. This is era-robust (old
    Huawei/Zepp days pick those sources) and locale-proof (no source-name match).
    avg/min/max/count are NOT deduped — extra samples don't inflate them.
    Pass `source` to force one source (the HKStatistics `separateBySource` escape
    hatch). ADR-0010.
    """
    if agg not in _AGG:
        raise ValueError(f"agg must be one of {sorted(_AGG)}")
    if bucket not in _BUCKET:
        raise ValueError(f"bucket must be one of {sorted(_BUCKET)}")
    b = _BUCKET[bucket]
    src_filter = " AND source=?" if source else ""
    src_arg = (source,) if source else ()

    if agg == "sum" and source is None:
        # ponytail: per-day dominant-source pick; upgrade to per-interval union
        # (Apple's exact merge) only if single-source days measurably undercount.
        rows = conn.execute(
            f"""
            WITH per_src AS (
                SELECT local_date, source, COUNT(*) n, SUM(CAST(value AS REAL)) s
                FROM record WHERE type=? AND local_date BETWEEN ? AND ?
                GROUP BY local_date, source),
            best AS (
                SELECT local_date, s,
                       ROW_NUMBER() OVER (PARTITION BY local_date ORDER BY n DESC, s DESC) rn
                FROM per_src)
            SELECT {b} period, ROUND(SUM(s), 2) value, COUNT(*) n
            FROM best WHERE rn=1 GROUP BY {b} ORDER BY period
            """,
            (type, start, end),
        ).fetchall()
        return [QuantityPoint(r["period"], r["value"], r["n"]) for r in rows]

    expr = "COUNT(*)" if agg == "count" else f"{agg}(CAST(value AS REAL))"
    rows = conn.execute(
        f"SELECT {b} period, {expr} value, COUNT(*) n "
        f"FROM record WHERE type=? AND local_date BETWEEN ? AND ?{src_filter} "
        f"GROUP BY {b} ORDER BY period",
        (type, start, end, *src_arg),
    ).fetchall()
    return [QuantityPoint(r["period"], r["value"], r["n"]) for r in rows]


def list_sources(conn: sqlite3.Connection, type: str) -> list[SourceInfo]:
    """Which sources wrote a given type, with counts — for transparency / `source=` use."""
    rows = conn.execute(
        "SELECT source, COUNT(*) n, MIN(local_date) first, MAX(local_date) last "
        "FROM record WHERE type=? GROUP BY source ORDER BY n DESC",
        (type,),
    ).fetchall()
    return [SourceInfo(r["source"], r["n"], r["first"], r["last"]) for r in rows]


def sleep(conn: sqlite3.Connection, start: str, end: str) -> list[SleepNight]:
    """Per-night sleep, attributed to the wake-up local day (end date).

    Reports minutes per stage. Legacy nights may only have InBed (older devices),
    so in_bed_min is surfaced as a fallback when asleep_min is 0.
    """
    asleep_in = ",".join("?" * len(_ASLEEP))
    rows = conn.execute(
        f"""
        SELECT substr(end,1,10) night,
               SUM(CASE WHEN value IN ({asleep_in}) THEN dur ELSE 0 END) asleep_min,
               SUM(CASE WHEN value='HKCategoryValueSleepAnalysisAsleepREM'  THEN dur ELSE 0 END) rem_min,
               SUM(CASE WHEN value='HKCategoryValueSleepAnalysisAsleepDeep' THEN dur ELSE 0 END) deep_min,
               SUM(CASE WHEN value='HKCategoryValueSleepAnalysisAsleepCore' THEN dur ELSE 0 END) core_min,
               SUM(CASE WHEN value='HKCategoryValueSleepAnalysisAwake'      THEN dur ELSE 0 END) awake_min,
               SUM(CASE WHEN value='HKCategoryValueSleepAnalysisInBed'      THEN dur ELSE 0 END) in_bed_min
        FROM (
            SELECT value, end,
                   (julianday(substr(end,1,19)) - julianday(substr(start,1,19)))*1440 dur
            FROM record
            WHERE type='HKCategoryTypeIdentifierSleepAnalysis'
              AND substr(end,1,10) BETWEEN ? AND ?
        )
        GROUP BY night ORDER BY night
        """,
        (*_ASLEEP, start, end),
    ).fetchall()
    return [
        SleepNight(
            night=r["night"],
            asleep_min=round(r["asleep_min"], 1),
            rem_min=round(r["rem_min"], 1),
            deep_min=round(r["deep_min"], 1),
            core_min=round(r["core_min"], 1),
            awake_min=round(r["awake_min"], 1),
            in_bed_min=round(r["in_bed_min"], 1),
        )
        for r in rows
    ]


# Unit -> factor to the canonical unit (ADR-0011). An unknown unit maps to NULL, so it
# drops out of the total instead of silently producing a wrong number.
_TO_MIN = {"min": 1.0, "hr": 60.0, "h": 60.0, "d": 1440.0, "s": 1 / 60, "ms": 1 / 60000}
_TO_KM = {"km": 1.0, "m": 0.001, "cm": 0.00001, "mi": 1.609344, "yd": 0.0009144, "ft": 0.0003048}
_TO_KCAL = {"kcal": 1.0, "Cal": 1.0, "cal": 0.001, "kJ": 1 / 4.184, "J": 1 / 4184}


def _factor(col: str, table: dict[str, float]) -> str:
    """SQL CASE mapping a unit column to its factor. Interpolates constants only."""
    whens = " ".join(f"WHEN '{u}' THEN {f!r}" for u, f in table.items())
    return f"(CASE {col} {whens} END)"


def _utc(col: str) -> str:
    """SQL julianday (UTC) of an Apple timestamp '2026-06-18 07:00:00 +0300'.

    Rewrites the offset as '+03:00', the form SQLite's date functions apply. Text
    comparison would be wrong across offsets. Interpolates column names only.
    """
    return f"julianday(substr({col}, 1, 19) || substr({col}, 21, 3) || ':' || substr({col}, 24, 2))"


# Stand-in for workout_stat on a DB built before it existed (0.1.1): the read-only
# server can't create the table, so stats read as NULL until the user re-ingests.
_NO_STATS = "(SELECT NULL workout_key, NULL type, NULL unit, NULL sum, NULL average WHERE 0)"


def _stats_table(conn: sqlite3.Connection) -> str:
    """`workout_stat`, or an empty stand-in on a DB built before it existed."""
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workout_stat'").fetchone()
    return "workout_stat" if has else _NO_STATS


def _workout_stats_cte(stats: str) -> str:
    """CTE `st`: per workout in CTE `w` (by `key`), km, active kcal and HR statistic.

    Shared by `workouts()` and `training_load()` so both normalize units the same way
    (ADR-0011). Interpolates constants and a table name only.
    """
    return f"""
st AS (
    SELECT workout_key,
           -- Legacy attribute only when the workout has no statistic: a later
           -- export of the same workout may add one (ADR-0011).
           COALESCE(
               SUM(CASE WHEN type LIKE 'HKQuantityTypeIdentifierDistance%'
                        THEN sum * {_factor("unit", _TO_KM)} END),
               MAX(CASE WHEN type = 'totalDistance' THEN sum * {_factor("unit", _TO_KM)} END)) km,
           COALESCE(
               SUM(CASE WHEN type = 'HKQuantityTypeIdentifierActiveEnergyBurned'
                        THEN sum * {_factor("unit", _TO_KCAL)} END),
               MAX(CASE WHEN type = 'totalEnergyBurned' THEN sum * {_factor("unit", _TO_KCAL)} END)) kcal,
           MAX(CASE WHEN type = 'HKQuantityTypeIdentifierHeartRate' THEN average END) hr
    FROM {stats} WHERE workout_key IN (SELECT key FROM w)
    GROUP BY workout_key)"""


def workouts(conn: sqlite3.Connection, start: str, end: str) -> list[WorkoutSummary]:
    """Per-activity workout summary in range: count, minutes, distance, energy, heart rate.

    `max_min` is the longest single workout, so a session left running (a forgotten
    stop) is visible instead of hiding in total_min/avg_min. Nothing is excluded.

    Durations are converted from `durationUnit` (missing unit = minutes, Apple's
    default). Distance sums every HKQuantityTypeIdentifierDistance* statistic (or the
    legacy totalDistance attribute) in km; energy is active energy in kcal. Average
    heart rate is weighted by workout duration over workouts that have one: the HR
    statistic, else the mean of HeartRate samples during the workout when there is at
    least one per minute of its start-to-end window (sparser readings leave it null).
    """
    rows = conn.execute(
        f"""
        WITH w AS (
            SELECT *,
                   -- julianday is a float: 30 min comes out as 30.0000002 without rounding.
                   ROUND((t1 - t0) * 1440, 3) win_min
            FROM (SELECT key, activity, local_date, end, {_utc("start")} t0, {_utc("end")} t1,
                         duration * {_factor("COALESCE(duration_unit, 'min')", _TO_MIN)} dur_min
                  FROM workout WHERE local_date BETWEEN ? AND ?)),
        {_workout_stats_cte(_stats_table(conn))},
        hs AS (
            -- No HR statistic: average the HeartRate samples recorded during the workout,
            -- compared as UTC instants. local_date only bounds the index scan (±1 day for
            -- a sample written under another offset).
            SELECT w.key, AVG(CAST(r.value AS REAL)) hr, COUNT(*) n
            FROM w JOIN record r
              ON r.type = 'HKQuantityTypeIdentifierHeartRate'
             AND r.local_date BETWEEN date(w.local_date, '-1 day') AND date(substr(w.end, 1, 10), '+1 day')
             AND {_utc("r.start")} BETWEEN w.t0 AND w.t1
            WHERE w.key NOT IN (SELECT workout_key FROM st WHERE hr IS NOT NULL)
            GROUP BY w.key),
        wh AS (
            SELECT w.*, st.km, st.kcal,
                   -- Sparse background readings don't describe a workout: need >= 1 sample
                   -- per minute of the [start, end] window they were drawn from (pauses too),
                   -- or of the duration if longer (a truncated window must not lower the bar).
                   -- Multi-arg MAX is NULL if either is: an unknown unit gets no fallback.
                   COALESCE(st.hr, CASE WHEN hs.n >= MAX(w.win_min, w.dur_min) THEN hs.hr END) hr
            FROM w LEFT JOIN st ON st.workout_key = w.key LEFT JOIN hs ON hs.key = w.key)
        SELECT activity, COUNT(*) n,
               ROUND(SUM(dur_min), 1) total_min,
               ROUND(AVG(dur_min), 1) avg_min,
               ROUND(MAX(dur_min), 1) max_min,
               ROUND(SUM(km), 2) total_distance_km,
               ROUND(SUM(kcal), 1) total_energy_kcal,
               ROUND(SUM(hr * dur_min) / SUM(CASE WHEN hr IS NOT NULL THEN dur_min END), 1) avg_heart_rate
        FROM wh
        GROUP BY activity ORDER BY n DESC
        """,
        (start, end),
    ).fetchall()
    return [
        WorkoutSummary(
            r["activity"],
            r["n"],
            r["total_min"],
            r["avg_min"],
            r["max_min"],
            r["total_distance_km"],
            r["total_energy_kcal"],
            r["avg_heart_rate"],
        )
        for r in rows
    ]


SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"
_MIN_Z_DAYS = 7  # fewer baseline days than this give no z: too few to estimate the spread
_CHRONIC_WEEKS = 4
MAX_LOAD_WEEKS = 104


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def daily_series(conn: sqlite3.Connection, type: str, start: str, end: str, agg: str = "avg") -> dict[str, float]:
    """One value per local day that has data: `quantity(..., agg, "day")`, or for
    `HKCategoryTypeIdentifierSleepAnalysis` minutes asleep per night (`agg` ignored)."""
    if type == SLEEP_TYPE:
        return {n.night: n.asleep_min for n in sleep(conn, start, end) if n.asleep_min > 0}
    return {p.period: p.value for p in quantity(conn, type, start, end, agg, "day")}


def baseline(
    conn: sqlite3.Connection,
    type: str,
    end: str,
    recent_days: int = 7,
    baseline_days: int = 28,
    agg: str = "avg",
) -> Baseline:
    """Compare the `recent_days` ending at `end` with the `baseline_days` just before them.

    Daily values come from `daily_series`, so `sum` keeps its per-day device dedupe
    (ADR-0010) and sleep is minutes asleep per night (reported as agg 'asleep_min').
    """
    if recent_days < 1 or baseline_days < 1:
        raise ValueError("recent_days and baseline_days must be at least 1")
    e = date.fromisoformat(end)
    r0 = e - timedelta(days=recent_days - 1)
    b1 = r0 - timedelta(days=1)
    b0 = b1 - timedelta(days=baseline_days - 1)
    daily = daily_series(conn, type, b0.isoformat(), end, agg)
    recent = [v for d, v in daily.items() if d >= r0.isoformat()]
    base = [v for d, v in daily.items() if d < r0.isoformat()]
    rm = statistics.fmean(recent) if recent else None
    bm = statistics.fmean(base) if base else None
    sd = statistics.stdev(base) if len(base) >= 2 else None
    delta = rm - bm if rm is not None and bm is not None else None
    return Baseline(
        type=type,
        agg="asleep_min" if type == SLEEP_TYPE else agg,
        recent_start=r0.isoformat(),
        recent_end=end,
        recent_mean=_r(rm),
        recent_n=len(recent),
        baseline_start=b0.isoformat(),
        baseline_end=b1.isoformat(),
        baseline_mean=_r(bm),
        baseline_sd=_r(sd),
        baseline_n=len(base),
        delta=_r(delta),
        delta_pct=_r(delta / bm * 100) if delta is not None and bm else None,
        z=_r(delta / sd) if delta is not None and sd and len(base) >= _MIN_Z_DAYS else None,
    )


def training_load(conn: sqlite3.Connection, end: str, weeks: int = 8) -> list[TrainingWeek]:
    """Workout volume per Monday–Sunday week, oldest first, ending with the week of `end`.

    The last week may be partial (it ends at its Sunday, not at `end`). Each week's
    `acwr` divides its minutes by the mean of the 4 weeks before it; those are read
    too, so every returned week has one. One query, with the same unit normalization
    as `workouts()` (ADR-0011) but without its heart-rate fallback, which load ignores.
    """
    if not 1 <= weeks <= MAX_LOAD_WEEKS:
        raise ValueError(f"weeks must be between 1 and {MAX_LOAD_WEEKS}")
    last = date.fromisoformat(week_range(end)[0])
    mondays = [last - timedelta(weeks=k) for k in range(weeks + _CHRONIC_WEEKS - 1, -1, -1)]
    first, final = mondays[0].isoformat(), week_range(end)[1]
    rows = conn.execute(
        f"""
        WITH w AS (
            SELECT key, local_date,
                   duration * {_factor("COALESCE(duration_unit, 'min')", _TO_MIN)} dur_min
            FROM workout WHERE local_date BETWEEN ? AND ?),
        {_workout_stats_cte(_stats_table(conn))}
        SELECT date(w.local_date, 'weekday 0', '-6 days') monday, COUNT(*) n,
               -- An unknown duration unit is NULL (ADR-0011): a session, but no minutes.
               COALESCE(SUM(dur_min), 0) mins, SUM(st.km) km, SUM(st.kcal) kcal
        FROM w LEFT JOIN st ON st.workout_key = w.key
        GROUP BY monday
        """,
        (first, final),
    ).fetchall()
    by_week = {r["monday"]: r for r in rows}
    mins = [by_week[m.isoformat()]["mins"] if m.isoformat() in by_week else 0 for m in mondays]
    out = []
    for i in range(_CHRONIC_WEEKS, len(mondays)):
        r = by_week.get(mondays[i].isoformat())
        chronic = statistics.fmean(mins[i - _CHRONIC_WEEKS : i])
        out.append(
            TrainingWeek(
                week_start=mondays[i].isoformat(),
                week_end=(mondays[i] + timedelta(days=6)).isoformat(),
                sessions=r["n"] if r else 0,
                total_min=round(mins[i], 1),
                # No workouts: 0. Workouts without distance/energy statistics: null.
                total_km=_r(r["km"]) if r else 0,
                total_kcal=_r(r["kcal"]) if r else 0,
                acwr=round(mins[i] / chronic, 2) if chronic else None,
            )
        )
    return out
