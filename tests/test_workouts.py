"""Workout statistics: parsing WorkoutStatistics (direct, nested, legacy) and unit math. ADR-0011."""

import zipfile
from datetime import datetime, timedelta

import pytest

from apple_health_export_mcp import queries
from apple_health_export_mcp.db import connect
from apple_health_export_mcp.ingest import ingest

RUN = "HKWorkoutActivityTypeRunning"
DIST = "HKQuantityTypeIdentifierDistanceWalkingRunning"
ENERGY = "HKQuantityTypeIdentifierActiveEnergyBurned"
HR = "HKQuantityTypeIdentifierHeartRate"
T0, T1 = "2026-06-18 07:00:00 +0300", "2026-06-18 07:30:00 +0300"


def _workout(body="", *, duration="30", unit="min", extra="", start=T0, end=T1, activity=RUN):
    return (
        f'<Workout workoutActivityType="{activity}" duration="{duration}" durationUnit="{unit}" '
        f'sourceName="Watch" startDate="{start}" endDate="{end}" {extra}>{body}</Workout>'
    )


def _stat(type, unit, start=T0, end=T1, **vals):
    attrs = " ".join(f'{k}="{v}"' for k, v in vals.items())
    return f'<WorkoutStatistics type="{type}" startDate="{start}" endDate="{end}" {attrs} unit="{unit}"/>'


def _ingest(tmp_path, *workouts):
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">' + "".join(workouts) + "</HealthData>"
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("apple_health_export/export.xml", xml)
    db = tmp_path / "health.db"
    counts = ingest(zip_path, db, progress=lambda *_: None)
    return connect(db, create=False), counts, zip_path, db


def _stats(conn):
    rows = conn.execute("SELECT type, unit, sum, average, minimum, maximum FROM workout_stat ORDER BY type").fetchall()
    return {r["type"]: tuple(r)[1:] for r in rows}


def test_direct_workout_statistics_are_stored(tmp_path):
    body = _stat(DIST, "km", sum="5") + _stat(HR, "count/min", average="150", minimum="120", maximum="175")
    conn, counts, *_ = _ingest(tmp_path, _workout(body))
    assert counts["workout_stat"] == 2
    assert _stats(conn) == {
        DIST: ("km", 5.0, None, None, None),
        HR: ("count/min", None, 150.0, 120.0, 175.0),
    }


def test_statistics_nested_in_workout_activity_are_stored(tmp_path):
    """iOS 16+ exports can put the statistics inside <WorkoutActivity>."""
    body = f'<WorkoutActivity uuid="A" startDate="{T0}" endDate="{T1}">{_stat(ENERGY, "kcal", sum="300")}</WorkoutActivity>'
    conn, *_ = _ingest(tmp_path, _workout(body))
    assert _stats(conn) == {ENERGY: ("kcal", 300.0, None, None, None)}


def test_direct_statistic_wins_over_nested_duplicate(tmp_path):
    nested = (
        f'<WorkoutActivity uuid="A" startDate="{T0}" endDate="{T1}">{_stat(DIST, "km", sum="4.9")}</WorkoutActivity>'
    )
    conn, *_ = _ingest(tmp_path, _workout(nested + _stat(DIST, "km", sum="5")))
    assert _stats(conn) == {DIST: ("km", 5.0, None, None, None)}


def test_nested_statistics_across_activities_are_merged(tmp_path):
    """Multi-activity workout, statistics only per activity: totals add up, HR is time-weighted."""
    mid = "2026-06-18 07:10:00 +0300"
    a = _stat(DIST, "km", end=mid, sum="2") + _stat(
        HR, "count/min", end=mid, average="120", minimum="100", maximum="130"
    )
    b = _stat(DIST, "km", start=mid, sum="3") + _stat(
        HR, "count/min", start=mid, average="150", minimum="125", maximum="170"
    )
    body = (
        f'<WorkoutActivity uuid="A" startDate="{T0}" endDate="{mid}">{a}</WorkoutActivity>'
        f'<WorkoutActivity uuid="B" startDate="{mid}" endDate="{T1}">{b}</WorkoutActivity>'
    )
    conn, *_ = _ingest(tmp_path, _workout(body))
    st = _stats(conn)
    assert st[DIST] == ("km", 5.0, None, None, None)
    unit, _sum, avg, lo, hi = st[HR]
    # 10 min at 120 + 20 min at 150 -> 140
    assert (unit, lo, hi) == ("count/min", 100.0, 170.0) and avg == pytest.approx(140.0)
    row = conn.execute("SELECT start, end FROM workout_stat WHERE type=?", (HR,)).fetchone()
    assert (row["start"], row["end"]) == (T0, T1)


def test_legacy_total_attributes_are_a_fallback(tmp_path):
    legacy = 'totalDistance="3.10686" totalDistanceUnit="mi" totalEnergyBurned="1255.2" totalEnergyBurnedUnit="kJ"'
    conn, *_ = _ingest(tmp_path, _workout(extra=legacy))
    assert _stats(conn) == {
        "totalDistance": ("mi", 3.10686, None, None, None),
        "totalEnergyBurned": ("kJ", 1255.2, None, None, None),
    }


def test_legacy_attributes_ignored_when_statistics_exist(tmp_path):
    legacy = 'totalDistance="5" totalDistanceUnit="km" totalEnergyBurned="300" totalEnergyBurnedUnit="kcal"'
    body = _stat(DIST, "km", sum="5") + _stat(ENERGY, "kcal", sum="300")
    conn, *_ = _ingest(tmp_path, _workout(body, extra=legacy))
    assert set(_stats(conn)) == {DIST, ENERGY}  # no double count


def test_reingest_does_not_duplicate_statistics(tmp_path):
    body = _stat(DIST, "km", sum="5") + _stat(ENERGY, "kcal", sum="300")
    conn, _, zip_path, db = _ingest(tmp_path, _workout(body))
    conn.close()
    ingest(zip_path, db, progress=lambda *_: None)
    conn = connect(db, create=False)
    assert conn.execute("SELECT COUNT(*) FROM workout_stat").fetchone()[0] == 2


def test_workout_summary_normalizes_units_and_weights_heart_rate(tmp_path):
    """mi->km, kJ->kcal, hr/s->min; avg HR weighted by workout duration."""
    t2, t3 = "2026-06-19 07:00:00 +0300", "2026-06-19 08:00:00 +0300"
    long_run = _workout(
        _stat(DIST, "mi", start=t2, end=t3, sum="6.21371")
        + _stat(ENERGY, "kJ", start=t2, end=t3, sum="2510.4")
        + _stat(HR, "count/min", start=t2, end=t3, average="140"),
        duration="1",
        unit="hr",
        start=t2,
        end=t3,
    )
    short_run = _workout(
        _stat(DIST, "km", sum="5") + _stat(ENERGY, "kcal", sum="300") + _stat(HR, "count/min", average="170"),
        duration="1800",
        unit="s",
    )
    legacy_walk = _workout(
        extra='totalDistance="1" totalDistanceUnit="km" totalEnergyBurned="50" totalEnergyBurnedUnit="Cal"',
        activity="HKWorkoutActivityTypeWalking",
    )
    conn, *_ = _ingest(tmp_path, long_run, short_run, legacy_walk)

    by = {w.activity: w for w in queries.workouts(conn, "2026-06-18", "2026-06-19")}
    run = by[RUN]
    assert run.n == 2
    assert run.total_min == 90.0 and run.avg_min == 45.0
    assert run.total_distance_km == pytest.approx(15.0, abs=0.01)
    assert run.total_energy_kcal == pytest.approx(900.0, abs=0.1)
    assert run.avg_heart_rate == pytest.approx((140 * 60 + 170 * 30) / 90, abs=0.1)

    walk = by["HKWorkoutActivityTypeWalking"]
    assert (walk.total_distance_km, walk.total_energy_kcal, walk.avg_heart_rate) == (1.0, 50.0, None)


def test_unknown_units_yield_null_not_a_wrong_number(tmp_path):
    body = _stat(DIST, "furlong", sum="9")
    conn, *_ = _ingest(tmp_path, _workout(body, unit="fortnight"))
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert (w.total_min, w.total_distance_km) == (None, None)


def test_summary_works_on_a_database_built_before_workout_stat(tmp_path):
    """0.1.1 DBs have no workout_stat and the read-only server can't add it."""
    conn, *_ = _ingest(tmp_path, _workout(duration="1", unit="hr"))
    conn.execute("DROP TABLE workout_stat")
    conn.commit()
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert (w.n, w.total_min, w.total_distance_km, w.total_energy_kcal, w.avg_heart_rate) == (1, 60.0, None, None, None)


def test_legacy_and_new_exports_of_one_workout_do_not_double_count(tmp_path):
    """Old export: attributes only. Newer export of the same workout: statistics. One DB."""
    legacy = 'totalDistance="5" totalDistanceUnit="km" totalEnergyBurned="300" totalEnergyBurnedUnit="kcal"'
    conn, _, _, db = _ingest(tmp_path, _workout(extra=legacy))
    conn.close()
    newer = tmp_path / "newer.zip"
    body = _stat(DIST, "km", sum="5") + _stat(ENERGY, "kcal", sum="300")
    with zipfile.ZipFile(newer, "w") as zf:
        zf.writestr("apple_health_export/export.xml", f"<HealthData>{_workout(body)}</HealthData>")
    ingest(newer, db, progress=lambda *_: None)

    conn = connect(db, create=False)
    assert conn.execute("SELECT COUNT(*) FROM workout").fetchone()[0] == 1
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert (w.total_distance_km, w.total_energy_kcal) == (5.0, 300.0)


def _two_segments(a, b, s0=T0, s1=T1, mid="2026-06-18 07:10:00 +0300", mid_b=None):
    return (
        f'<WorkoutActivity uuid="A" startDate="{s0}" endDate="{mid}">{a}</WorkoutActivity>'
        f'<WorkoutActivity uuid="B" startDate="{mid_b or mid}" endDate="{s1}">{b}</WorkoutActivity>'
    )


def test_merged_segments_with_mixed_units_yield_null_not_a_wrong_sum(tmp_path):
    body = _two_segments(_stat(DIST, "km", sum="2"), _stat(DIST, "m", sum="3000"))
    conn, *_ = _ingest(tmp_path, _workout(body))
    assert _stats(conn)[DIST][0] is None  # unit unknown -> normalizes to NULL (ADR-0011)
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert w.total_distance_km is None


def test_merged_span_is_ordered_by_time_not_by_string(tmp_path):
    """07:00 +0300 is earlier than 05:30 +0100 (= 07:30 +0300), though it sorts later as text."""
    early, late_other_tz = "2026-06-18 07:00:00 +0300", "2026-06-18 05:30:00 +0100"
    a = _stat(DIST, "km", start=early, end="2026-06-18 07:10:00 +0300", sum="2")
    b = _stat(DIST, "km", start=late_other_tz, end=T1, sum="3")
    conn, *_ = _ingest(tmp_path, _workout(_two_segments(a, b)))
    row = conn.execute("SELECT start, end FROM workout_stat").fetchone()
    assert (row["start"], row["end"]) == (early, T1)


def test_malformed_numbers_do_not_abort_ingest(tmp_path):
    bad = _workout(duration="n/a", extra='totalDistance="abc" totalDistanceUnit="km"')
    good = _workout(_stat(DIST, "km", sum="oops") + _stat(ENERGY, "kcal", sum="300"), start=T1, end=T1)
    conn, counts, *_ = _ingest(tmp_path, bad, good)
    assert counts["workout"] == 2
    assert conn.execute("SELECT COUNT(*) FROM workout_stat WHERE type='totalDistance'").fetchone()[0] == 0
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert w.total_energy_kcal == 300.0


@pytest.mark.parametrize(
    ("duration", "unit", "minutes"), [("0.5", "d", 720.0), ("1800000", "ms", 30.0), ("1.5", "hr", 90.0)]
)
def test_duration_units(tmp_path, duration, unit, minutes):
    conn, *_ = _ingest(tmp_path, _workout(duration=duration, unit=unit))
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert w.total_min == minutes


@pytest.mark.parametrize(("value", "unit"), [("5", "km"), ("5000", "m"), ("500000", "cm"), ("16404.2", "ft")])
def test_distance_units(tmp_path, value, unit):
    conn, *_ = _ingest(tmp_path, _workout(_stat(DIST, unit, sum=value)))
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert w.total_distance_km == pytest.approx(5.0, abs=0.01)


# --- Heart-rate fallback from HeartRate samples (issue #4) -----------------------


def _hr_samples(values, start="2026-06-18 07:00:30", offset="+0300", step_s=60):
    """HeartRate <Record>s, one every `step_s` seconds from `start` (local time + offset)."""
    t0 = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    out = []
    for i, v in enumerate(values):
        ts = (t0 + timedelta(seconds=i * step_s)).strftime("%Y-%m-%d %H:%M:%S") + f" {offset}"
        out.append(
            f'<Record type="{HR}" sourceName="Watch" unit="count/min" value="{v}" startDate="{ts}" endDate="{ts}"/>'
        )
    return "".join(out)


def _hr(conn, day="2026-06-18"):
    (w,) = queries.workouts(conn, day, day)
    return w.avg_heart_rate


def test_heart_rate_falls_back_to_dense_samples(tmp_path):
    """No HR statistic, 30 samples over a 30-min workout (1/min) -> their mean."""
    conn, *_ = _ingest(tmp_path, _workout(), _hr_samples([140, 160] * 15))
    assert _hr(conn) == 150.0


def test_sparse_samples_leave_heart_rate_null(tmp_path):
    """29 samples over 30 min is below 1/min: background readings, not a workout trace."""
    conn, *_ = _ingest(tmp_path, _workout(), _hr_samples([150] * 29))
    assert _hr(conn) is None


def test_heart_rate_statistic_wins_over_samples(tmp_path):
    body = _stat(HR, "count/min", average="150")
    conn, *_ = _ingest(tmp_path, _workout(body), _hr_samples([100] * 30))
    assert _hr(conn) == 150.0


def test_samples_outside_the_workout_are_excluded(tmp_path):
    inside = _hr_samples([150] * 30)
    before = _hr_samples([60] * 30, start="2026-06-18 06:00:00")
    after = _hr_samples([60] * 30, start="2026-06-18 07:31:00")
    conn, *_ = _ingest(tmp_path, _workout(), before, inside, after)
    assert _hr(conn) == 150.0


def test_samples_are_matched_by_instant_not_by_text(tmp_path):
    """04:00:30 +0000 is inside 07:00-07:30 +0300; 07:00:30 +0000 (10:00:30 +0300) is not."""
    same_instant = _hr_samples([150] * 30, start="2026-06-18 04:00:30", offset="+0000")
    same_text = _hr_samples([60] * 30, start="2026-06-18 07:00:30", offset="+0000")
    conn, *_ = _ingest(tmp_path, _workout(), same_instant, same_text)
    assert _hr(conn) == 150.0


def test_samples_on_both_sides_of_midnight_are_used(tmp_path):
    late, early = "2026-06-18 23:45:00 +0300", "2026-06-19 00:15:00 +0300"
    samples = _hr_samples([140] * 15, start="2026-06-18 23:45:30") + _hr_samples(
        [160] * 15, start="2026-06-19 00:00:30"
    )
    conn, *_ = _ingest(tmp_path, _workout(start=late, end=early), samples)
    assert _hr(conn) == 150.0


# --- Longest workout per activity (issue #5) --------------------------------------


def test_max_min_exposes_a_workout_left_running(tmp_path):
    """A forgotten stop inflates the totals; max_min lets the caller see it."""
    t2, t3 = "2026-06-19 07:00:00 +0300", "2026-06-22 07:00:00 +0300"
    forgotten = _workout(duration="72", unit="hr", start=t2, end=t3)
    conn, *_ = _ingest(tmp_path, _workout(), _workout(duration="1800", unit="s", start=T1, end=T1), forgotten)
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-19")
    assert (w.n, w.total_min, w.avg_min, w.max_min) == (3, 4380.0, 1460.0, 4320.0)


def test_max_min_is_null_when_no_duration_is_known(tmp_path):
    conn, *_ = _ingest(tmp_path, _workout(unit="fortnight"))
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert w.max_min is None


def test_density_is_measured_over_the_whole_window_not_active_time(tmp_path):
    """Paused workout: 20 active min in a 60-min window; 20 samples is 1 per 3 min of window."""
    paused = _workout(duration="20", end="2026-06-18 08:00:00 +0300")
    conn, *_ = _ingest(tmp_path, paused, _hr_samples([150] * 20, step_s=180))
    assert _hr(conn) is None


def test_samples_dated_the_day_before_under_another_offset(tmp_path):
    """01:00 +0300 workout; its samples, written under +0000, fall on the previous local day."""
    early = _workout(start="2026-06-18 01:00:00 +0300", end="2026-06-18 01:30:00 +0300")
    samples = _hr_samples([150] * 30, start="2026-06-17 22:00:30", offset="+0000")
    conn, *_ = _ingest(tmp_path, early, samples)
    assert _hr(conn) == 150.0


def test_samples_dated_the_day_after_under_another_offset(tmp_path):
    """23:00 +0000 workout; its samples, written under +0300, fall on the next local day."""
    late = _workout(start="2026-06-18 23:00:00 +0000", end="2026-06-18 23:30:00 +0000")
    samples = _hr_samples([150] * 30, start="2026-06-19 02:00:30", offset="+0300")
    conn, *_ = _ingest(tmp_path, late, samples)
    assert _hr(conn) == 150.0


def test_multi_day_workout_uses_samples_from_its_last_day(tmp_path):
    """48 h window: the scan must reach the end date, not just the start date + 1."""
    long = _workout(duration="48", unit="hr", end="2026-06-20 07:00:00 +0300")
    samples = _hr_samples([150] * 3000, start="2026-06-20 00:00:00", step_s=5)
    conn, *_ = _ingest(tmp_path, long, samples)
    assert _hr(conn) == 150.0


def test_fallback_alongside_other_statistics(tmp_path):
    conn, *_ = _ingest(tmp_path, _workout(_stat(DIST, "km", sum="5")), _hr_samples([150] * 30))
    (w,) = queries.workouts(conn, "2026-06-18", "2026-06-18")
    assert (w.total_distance_km, w.avg_heart_rate) == (5.0, 150.0)


def test_fallback_on_a_database_built_before_workout_stat(tmp_path):
    conn, *_ = _ingest(tmp_path, _workout(), _hr_samples([150] * 30))
    conn.execute("DROP TABLE workout_stat")
    conn.commit()
    assert _hr(conn) == 150.0
