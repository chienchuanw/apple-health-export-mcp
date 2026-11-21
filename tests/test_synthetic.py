"""End-to-end ground truth on a 30-day synthetic export (see tests/fixtures/synthetic.py)."""

from datetime import date, timedelta

import pytest
from fixtures.synthetic import IPHONE, WATCH, write_export

from apple_health_export_mcp import queries
from apple_health_export_mcp.db import connect
from apple_health_export_mcp.ingest import ingest

N_DAYS = 30
FIRST, LAST = "2026-10-01", "2026-10-30"
DAYS = [(date(2026, 10, 1) + timedelta(days=i)).isoformat() for i in range(N_DAYS)]
STEPS = "HKQuantityTypeIdentifierStepCount"


def _build(tmp_path, **kw):
    zip_path = write_export(tmp_path / "export.zip", N_DAYS, **kw)
    db = tmp_path / "health.db"
    counts = ingest(zip_path, db, progress=lambda *_: None)
    return zip_path, db, counts


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    zip_path, db, counts = _build(tmp_path_factory.mktemp("synthetic"))
    conn = connect(db, create=False)
    yield conn, zip_path, db, counts
    conn.close()


def test_steps_dedupe_parallel_devices_per_local_day(synthetic):
    conn, *_ = synthetic
    out = queries.quantity(conn, STEPS, "2026-09-30", LAST, "sum", "day")
    # Every local day is 2000 (not 4000), and nothing leaks to 09-30: the 07:30 +0800
    # sample is 23:30 UTC the previous day, so a UTC split would be wrong here.
    assert [(r.period, r.value) for r in out] == [(d, 2000.0) for d in DAYS]
    naive = conn.execute(
        "SELECT SUM(CAST(value AS REAL)) FROM record WHERE type=? AND local_date=?", (STEPS, FIRST)
    ).fetchone()[0]
    assert naive == 4000.0
    for src in (IPHONE, WATCH):
        (one,) = queries.quantity(conn, STEPS, FIRST, FIRST, "sum", "day", source=src)
        assert one.value == 2000.0


def test_hrv_with_nested_metadata_is_parsed(synthetic):
    conn, *_ = synthetic
    (r,) = queries.quantity(conn, "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", FIRST, LAST, "avg", "all")
    assert (r.value, r.n) == (45.0, N_DAYS)


def test_sleep_stages_per_night_attributed_to_wake_day(synthetic):
    conn, *_ = synthetic
    nights = queries.sleep(conn, FIRST, LAST)
    assert [n.night for n in nights] == DAYS
    for n in nights:
        assert (n.asleep_min, n.rem_min, n.deep_min, n.core_min, n.awake_min, n.in_bed_min) == (
            420.0,
            120.0,
            60.0,
            240.0,
            10.0,
            495.0,
        )


def test_one_run_per_day_with_distance_energy_heart_rate(synthetic):
    conn, *_ = synthetic
    for d in (DAYS[0], DAYS[14], DAYS[-1]):
        (w,) = queries.workouts(conn, d, d)
        assert (w.activity, w.n, w.total_min, w.total_distance_km, w.total_energy_kcal, w.avg_heart_rate) == (
            "HKWorkoutActivityTypeRunning",
            1,
            30.0,
            5.0,
            300.0,
            150.0,
        )


def test_thirty_day_workout_totals(synthetic):
    conn, *_ = synthetic
    (w,) = queries.workouts(conn, FIRST, LAST)
    assert (w.n, w.total_min, w.max_min, w.total_distance_km, w.total_energy_kcal, w.avg_heart_rate) == (
        30,
        900.0,
        30.0,
        150.0,
        9000.0,
        150.0,
    )


def test_every_workout_has_its_three_statistics(synthetic):
    conn, *_ = synthetic
    assert conn.execute("SELECT COUNT(*) FROM workout_stat").fetchone()[0] == N_DAYS * 3
    missing = conn.execute(
        "SELECT COUNT(*) FROM workout w WHERE (SELECT COUNT(*) FROM workout_stat s WHERE s.workout_key=w.key) != 3"
    ).fetchone()[0]
    assert missing == 0


def test_activity_summary_and_routes_are_skipped(synthetic):
    conn, *_ = synthetic
    types = {t.type for t in queries.list_types(conn)}
    assert types == {
        STEPS,
        "HKQuantityTypeIdentifierHeartRateVariabilitySDNN",
        "HKCategoryTypeIdentifierSleepAnalysis",
        "Workout",
    }


def test_reingest_keeps_every_count(synthetic):
    conn, zip_path, db, first = synthetic
    assert first == {"record": N_DAYS * 10, "workout": N_DAYS, "workout_stat": N_DAYS * 3}

    def table_counts():
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in first}

    before = table_counts()
    ingest(zip_path, db, progress=lambda *_: None)
    assert table_counts() == before == first


def test_imperial_units_give_the_same_ground_truth(tmp_path):
    """Same run written as 0.5 hr / 3.10686 mi / 1255.2 kJ normalizes back to min/km/kcal."""
    _, db, _ = _build(tmp_path, imperial=True)
    conn = connect(db, create=False)
    (w,) = queries.workouts(conn, FIRST, LAST)
    assert (w.n, w.total_min, w.avg_heart_rate) == (30, 900.0, 150.0)
    assert w.total_distance_km == pytest.approx(150.0, abs=0.01)
    assert w.total_energy_kcal == pytest.approx(9000.0, abs=0.1)
    conn.close()
