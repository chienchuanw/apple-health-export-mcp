"""Workout statistics: parsing WorkoutStatistics (direct, nested, legacy) and unit math. ADR-0011."""

import zipfile

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
