"""Personal baselines and weekly training load (ADR-0017)."""

import asyncio
import sqlite3
import statistics
from datetime import date, timedelta

import pytest
from fastmcp import Client
from fixtures.synthetic import write_export

from apple_health_export_mcp import prompts, queries, server, tools
from apple_health_export_mcp.db import connect_readonly, init_schema
from apple_health_export_mcp.ingest import ingest

RHR = "HKQuantityTypeIdentifierRestingHeartRate"
HRV = "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"
STEPS = "HKQuantityTypeIdentifierStepCount"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
END = date(2026, 6, 30)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def _day(n_back: int) -> str:
    """The date `n_back` days before END (0 = END)."""
    return (END - timedelta(days=n_back)).isoformat()


def _rec(conn, type_, value, day, source="Watch", hour=7):
    start = f"{day} {hour:02d}:00:00 +0300"
    conn.execute(
        "INSERT INTO record(key,type,source,value,start,end,local_date) VALUES (?,?,?,?,?,?,?)",
        (f"{type_}{source}{start}".encode(), type_, source, str(value), start, start, day),
    )


def _workout(conn, day, minutes, activity="HKWorkoutActivityTypeRunning"):
    start = f"{day} 06:00:00 +0300"
    conn.execute(
        "INSERT INTO workout(key,activity,duration,duration_unit,start,end,local_date) VALUES (?,?,?,?,?,?,?)",
        (f"{day}{activity}".encode(), activity, minutes, "min", start, start, day),
    )


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    """40 synthetic days, 2026-10-01..2026-11-09: HRV 45 and 420 min asleep every day."""
    tmp = tmp_path_factory.mktemp("baseline")
    db = tmp / "health.db"
    ingest(write_export(tmp / "export.zip", 40), db, progress=lambda *_: None)
    conn = connect_readonly(db)
    yield conn, db
    conn.close()


# --- get_baseline --------------------------------------------------------------------


def test_constant_series_has_zero_delta_and_no_z(synth):
    conn, _ = synth
    b = queries.baseline(conn, HRV, "2026-11-09")
    assert (b.recent_mean, b.baseline_mean, b.delta, b.delta_pct) == (45, 45, 0, 0)
    assert (b.recent_n, b.baseline_n) == (7, 28)
    assert (b.recent_start, b.recent_end) == ("2026-11-03", "2026-11-09")
    assert (b.baseline_start, b.baseline_end) == ("2026-10-06", "2026-11-02")
    assert b.baseline_sd == 0 and b.z is None  # zero spread: no meaningful z


def test_rhr_shift_gives_delta_pct_and_z():
    conn = _db()
    base = [50 if i % 2 else 52 for i in range(28)]
    for i, v in enumerate(base):
        _rec(conn, RHR, v, _day(7 + i))  # the 28 days before the recent week
    for i in range(7):
        _rec(conn, RHR, 55, _day(i))
    b = queries.baseline(conn, RHR, END.isoformat())
    sd = statistics.stdev(base)
    assert b.recent_mean == 55 and b.baseline_mean == 51
    assert b.delta == 4 and b.delta_pct == pytest.approx(7.84, abs=0.01)
    assert b.baseline_sd == pytest.approx(sd, abs=0.01)
    assert b.z == pytest.approx(4 / sd, abs=0.01)


def test_z_needs_at_least_a_week_of_baseline_days():
    conn = _db()
    for i, v in enumerate([50, 53, 51, 52, 50]):  # only 5 baseline days
        _rec(conn, RHR, v, _day(7 + i))
    _rec(conn, RHR, 56, _day(0))
    b = queries.baseline(conn, RHR, END.isoformat())
    assert b.baseline_n == 5 and b.baseline_sd is not None and b.z is None
    assert b.recent_n == 1 and b.recent_mean == 56


def test_no_data_gives_nulls_not_zeros():
    b = queries.baseline(_db(), RHR, END.isoformat())
    assert (b.recent_mean, b.baseline_mean, b.delta, b.delta_pct, b.z) == (None,) * 5
    assert (b.recent_n, b.baseline_n) == (0, 0)


def test_sum_baseline_uses_deduped_daily_totals():
    conn = _db()
    for i in range(35):  # iPhone and Watch both log the same 3000 steps every day
        _rec(conn, STEPS, 3000, _day(i), source="iPhone")
        _rec(conn, STEPS, 3000, _day(i), source="Watch")
    b = queries.baseline(conn, STEPS, END.isoformat(), agg="sum")
    assert b.recent_mean == 3000 and b.baseline_mean == 3000  # not 6000


def test_sleep_baseline_uses_minutes_asleep_per_night(synth):
    conn, _ = synth
    b = queries.baseline(conn, SLEEP, "2026-11-09")
    assert (b.recent_mean, b.baseline_mean, b.recent_n, b.baseline_n) == (420, 420, 7, 28)


def test_custom_windows():
    conn = _db()
    for i in range(10):
        _rec(conn, RHR, 60 if i < 3 else 50, _day(i))
    b = queries.baseline(conn, RHR, END.isoformat(), recent_days=3, baseline_days=7)
    assert (b.recent_mean, b.baseline_mean, b.recent_n, b.baseline_n) == (60, 50, 3, 7)


@pytest.mark.parametrize("kw", [{"agg": "median"}, {"recent_days": 0}, {"baseline_days": 0}])
def test_bad_arguments_are_value_errors(kw):
    with pytest.raises(ValueError):
        queries.baseline(_db(), RHR, END.isoformat(), **kw)


# --- get_training_load ---------------------------------------------------------------


def test_training_load_is_one_row_per_week_oldest_first_with_acwr():
    conn = _db()
    monday = date(2026, 6, 29)  # the week containing END (2026-06-30)
    # Four chronic weeks of 100 min each, then 200 min in the last week -> ACWR 2.0.
    for w in range(1, 5):
        _workout(conn, (monday - timedelta(weeks=w)).isoformat(), 100)
    _workout(conn, monday.isoformat(), 120)
    _workout(conn, (monday + timedelta(days=1)).isoformat(), 80)
    weeks = queries.training_load(conn, END.isoformat(), weeks=2)
    assert [w.week_start for w in weeks] == ["2026-06-22", "2026-06-29"]
    assert [w.week_end for w in weeks] == ["2026-06-28", "2026-07-05"]
    last = weeks[-1]
    assert (last.sessions, last.total_min, last.acwr) == (2, 200, 2.0)
    # The week before had 100 min against a chronic mean of (100+100+100+0)/4 = 75.
    assert weeks[0].total_min == 100 and weeks[0].acwr == pytest.approx(1.33, abs=0.01)


def test_week_without_workouts_is_zero_and_acwr_null_without_history():
    weeks = queries.training_load(_db(), END.isoformat(), weeks=1)
    assert len(weeks) == 1
    w = weeks[0]
    assert (w.sessions, w.total_min, w.total_km, w.total_kcal, w.acwr) == (0, 0, 0, 0, None)


def test_training_load_on_synthetic_runs(synth):
    conn, _ = synth
    weeks = queries.training_load(conn, "2026-11-08", weeks=4)  # Sunday: full weeks
    assert [w.week_start for w in weeks] == ["2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02"]
    assert all((w.sessions, w.total_min, w.total_km, w.total_kcal) == (7, 210, 35, 2100) for w in weeks)
    # The last week's 4 prior weeks are all full (210 min): steady load. Earlier rows
    # reach back to before the data starts (2026-10-01), so their chronic mean is lower.
    assert weeks[-1].acwr == 1.0 and weeks[0].acwr > 1


def test_training_load_rejects_a_non_positive_week_count():
    with pytest.raises(ValueError):
        queries.training_load(_db(), END.isoformat(), weeks=0)


# --- tool surface and prompts --------------------------------------------------------


def test_tools_read_through_the_server_connection(synth, monkeypatch):
    _, db = synth
    monkeypatch.setenv("AH_DB_PATH", str(db))
    assert tools.get_baseline(HRV, "2026-11-09").baseline_mean == 45
    assert tools.get_training_load("2026-11-08", weeks=1)[0].total_min == 210


def test_bad_tool_arguments_are_tool_errors(synth, monkeypatch):
    from fastmcp.exceptions import ToolError

    _, db = synth
    monkeypatch.setenv("AH_DB_PATH", str(db))
    with pytest.raises(ToolError):
        tools.get_baseline(HRV, "2026-11-09", agg="median")


def test_both_tools_are_registered_with_output_schemas():
    async def listed():
        async with Client(server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    found = asyncio.run(listed())
    for name in ("get_baseline", "get_training_load"):
        assert name in found and found[name].outputSchema


def test_coaching_prompts_use_the_new_tools():
    assert "get_baseline" in prompts.readiness_check()
    assert "get_training_load" in prompts.weekly_review()
