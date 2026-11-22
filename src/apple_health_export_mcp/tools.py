"""MCP tool surface — thin wrappers over `queries.py` (where the testable SQL lives).

Like `prompts.py`, this stays free of the FastMCP server instance: the functions are
plain (un-decorated) and `server.py` registers each via `mcp.tool(...)`. Tools return
aggregates only (ADR-0008) and the typed models from `queries.py`, so FastMCP derives
an output schema per tool. The DB must already exist — build it with
`apple-health-export-mcp-ingest <export.zip>` (ADR-0006).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Annotated, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from apple_health_export_mcp import queries
from apple_health_export_mcp.db import connect_readonly
from apple_health_export_mcp.sql import MAX_ROWS, SqlResult, run_select

# All tools only read a local, fixed DB: safe to retry, no side effects, no open world.
READONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}

# --- Reusable, self-documenting argument types (FastMCP Field metadata) ------
TypeId = Annotated[
    str,
    Field(
        description="HealthKit type identifier. Discover valid values with `list_types`.",
        examples=["HKQuantityTypeIdentifierStepCount", "HKQuantityTypeIdentifierRestingHeartRate"],
    ),
]
DateStr = Annotated[
    str,
    Field(
        description="Local calendar date, ISO 'YYYY-MM-DD'.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        examples=["2026-06-30"],
    ),
]
Agg = Annotated[
    Literal["sum", "avg", "min", "max", "count"],
    Field(
        description="Aggregation: 'sum' for cumulative metrics (steps, energy), "
        "'avg' for sampled ones (heart rate, weight)."
    ),
]
Bucket = Annotated[
    Literal["day", "week", "month", "all"],
    Field(description="Group results by this time bucket."),
]
Source = Annotated[
    str | None,
    Field(description="Restrict to one source/device (see `list_sources`). Omit to auto-dedupe parallel devices."),
]


@contextmanager
def _store():
    """Open the health DB for the duration of one tool call.

    Read-only (`mode=ro`): the server can never modify the DB, even via a bug.
    Surfaces a clean ToolError (instead of a raw traceback) when the DB hasn't
    been built yet, an argument is rejected (ValueError) or a query fails, and
    always closes the connection.
    """
    try:
        conn = connect_readonly()
    except (FileNotFoundError, sqlite3.Error) as e:
        raise ToolError(str(e)) from e
    try:
        yield conn
    except ValueError as e:  # a bad argument (agg, bucket, window size...) from queries/sql
        raise ToolError(str(e)) from e
    except sqlite3.Error as e:
        raise ToolError(f"Database error: {e}. Re-run `apple-health-export-mcp-ingest` if it persists.") from e
    finally:
        conn.close()


def list_types() -> list[queries.TypeInfo]:
    """List which health metrics exist in this export, with row counts and date spans.

    Call this first to discover the exact `type` strings to pass to `get_quantity`.
    """
    with _store() as conn:
        return queries.list_types(conn)


def list_sources(type: TypeId) -> list[queries.SourceInfo]:
    """List which sources/devices wrote a metric, with counts and date spans.

    Use it to see why a `sum` differs across devices, or to pick a `source` for `get_quantity`.
    """
    with _store() as conn:
        return queries.list_sources(conn, type)


def get_quantity(
    type: TypeId,
    start: DateStr,
    end: DateStr,
    agg: Agg = "sum",
    bucket: Bucket = "day",
    source: Source = None,
) -> list[queries.QuantityPoint]:
    """Aggregate a numeric metric (steps, weight, heart rate, energy…) over a date range.

    Call `list_types` first to find the exact `type` string. Pick `agg` by metric kind:
    `sum` for cumulative (steps, active energy), `avg` for sampled (weight, heart rate).
    `sum` auto-deduplicates parallel devices (Watch + iPhone + apps) per day, so it does
    not over-count (ADR-0010); pass `source` to force one device. `avg/min/max` are not
    deduped. Returns [{period, value, n}].
    """
    with _store() as conn:
        return queries.quantity(conn, type, start, end, agg, bucket, source)


def get_sleep(start: DateStr, end: DateStr) -> list[queries.SleepNight]:
    """Per-night sleep stage durations (minutes), attributed to the wake-up day.

    Returns [{night, asleep_min, rem_min, deep_min, core_min, awake_min, in_bed_min}].
    Older nights may report only in_bed_min (legacy devices lack stage detail).
    """
    with _store() as conn:
        return queries.sleep(conn, start, end)


def get_workouts(start: DateStr, end: DateStr) -> list[queries.WorkoutSummary]:
    """Workout summary per activity type in a date range.

    Returns [{activity, n, total_min, avg_min, max_min, total_distance_km,
    total_energy_kcal, avg_heart_rate}]. Units are normalized (km, kcal, minutes).
    avg_heart_rate (bpm) is weighted by workout duration; it comes from the workout's
    heart-rate statistic or, without one, from heart-rate samples recorded during it
    (only if there is at least one per minute of its start-to-end window). A field is
    null when the export has no such data. max_min is the longest single workout: if it is far above avg_min, a
    session was probably left running and skews total_min/avg_min, so call it out
    instead of taking the totals at face value.
    """
    with _store() as conn:
        return queries.workouts(conn, start, end)


SqlQuery = Annotated[
    str,
    Field(
        description="Exactly one SQLite SELECT (WITH … SELECT is fine). Aggregate in SQL; "
        f"at most {MAX_ROWS} rows come back.",
        examples=[
            "SELECT local_date, SUM(CAST(value AS REAL)) FROM record "
            "WHERE type = 'HKQuantityTypeIdentifierStepCount' AND source LIKE '%Watch%' "
            "AND local_date BETWEEN '2026-06-01' AND '2026-06-30' GROUP BY local_date"
        ],
    ),
]


def query_sql(sql: SqlQuery) -> SqlResult:
    """Run one read-only SELECT on the health DB, for questions the other tools cannot answer.

    Opt-in (AH_ENABLE_SQL=1). Prefer the other tools when they fit: their numbers are
    deduplicated and unit-normalized, raw SQL is not. Tables (all columns):
    - record(key, type, source, unit, value, start, end, created, local_date): one row
      per sample. `value` is TEXT, so use CAST(value AS REAL) for numbers (category types
      such as sleep hold enum strings). `local_date` is the local 'YYYY-MM-DD' day;
      filter on (type, local_date), which is indexed. Find types with `list_types`.
    - workout(key, activity, duration, duration_unit, source, start, end, created, local_date).
    - workout_stat(workout_key, type, unit, sum, average, minimum, maximum, start, end):
      per-workout distance/energy/heart rate in raw units (km or mi, kcal or kJ);
      workout_key = workout.key.
    - meta(k, v): ingest bookkeeping.
    `start`/`end` are local times with the offset ('2026-06-29 08:14:22 +0300').
    Watch, iPhone and apps record in parallel, so SUM over record without a `source`
    filter double-counts (`get_quantity` dedupes this). Source names are personal
    device names: look them up with `list_sources`, don't guess them. Only SELECT runs: writes,
    PRAGMA and ATTACH are refused; read `sqlite_master` for the full schema. A query
    is stopped after 5 s. Returns {columns, rows, truncated}; truncated is true when
    more rows matched than were returned.
    """
    with _store() as conn:
        return run_select(conn, sql)


def get_baseline(
    type: TypeId,
    end: DateStr,
    recent_days: Annotated[int, Field(ge=1, le=90, description="Days in the recent window, ending at `end`.")] = 7,
    baseline_days: Annotated[
        int, Field(ge=1, le=365, description="Days in the baseline window, right before the recent one.")
    ] = 28,
    agg: Agg = "avg",
) -> queries.Baseline:
    """Compare a metric's recent days with the user's own baseline just before them.

    Use it for recovery markers: resting heart rate and HRV (`avg`), steps (`sum`), or
    sleep via `HKCategoryTypeIdentifierSleepAnalysis` (minutes asleep per night; `agg`
    is ignored). Each window's mean is over its days with data, from the same per-day
    values as `get_quantity` (so `sum` is deduped across devices). Returns the two
    windows, recent_mean, baseline_mean, baseline_sd, delta, delta_pct, and
    z = delta / baseline_sd. z is null with under 7 baseline days or zero spread. A
    |z| above ~1 is a notable move against the user's normal. ADR-0017.
    """
    with _store() as conn:
        return queries.baseline(conn, type, end, recent_days, baseline_days, agg)


def get_training_load(
    end: DateStr,
    weeks: Annotated[
        int, Field(ge=1, le=queries.MAX_LOAD_WEEKS, description="Weeks to return, ending with the week of `end`.")
    ] = 8,
) -> list[queries.TrainingWeek]:
    """Workout volume per Monday–Sunday week, oldest first, with an acute:chronic ratio.

    Each week: sessions, total_min, total_km, total_kcal (units normalized as in
    `get_workouts`) and acwr = this week's minutes / the mean of the 4 weeks before it
    (null when that mean is 0). An acwr well above ~1.3 is a sharp load jump. Load is
    minutes-based: the export has no per-workout intensity score. Nothing after `end`
    is counted: the last week runs Monday to `end`, so it is partial (and its acwr
    reads low) unless `end` is a Sunday. ADR-0017.
    """
    with _store() as conn:
        return queries.training_load(conn, end, weeks)


# Registered by server.py via mcp.tool(annotations=READONLY).
ALL = [list_types, list_sources, get_quantity, get_sleep, get_workouts, get_baseline, get_training_load]


def enabled() -> list:
    """The tools to register: ALL, plus `query_sql` only when AH_ENABLE_SQL=1 (ADR-0013)."""
    return [*ALL, query_sql] if os.environ.get("AH_ENABLE_SQL") == "1" else list(ALL)
