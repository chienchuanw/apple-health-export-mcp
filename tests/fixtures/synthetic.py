"""Realistic synthetic Apple Health export (Asia/Taipei, +0800) with known ground truth.

Per local day, starting 2026-10-01:
- Steps: iPhone and Apple Watch each log 1000 at 07:30 and 12:00 -> deduped sum 2000
  (a naive SUM over both sources gives 4000). 07:30 +0800 is 23:30 UTC the previous
  day, so a UTC-based day split would put half the steps on the wrong day.
- HRV 45 ms with a nested <HeartRateVariabilityMetadataList>.
- Sleep for the night ending that day (attributed to the wake-up day):
  Core 4h + Deep 1h + REM 2h = asleep 420 min, Awake 10 min, iPhone InBed 8h15m = 495 min.
- One iOS 16-style run: 30 min, 5 km, 300 kcal, avg HR 150, with statistics nested in
  <WorkoutActivity> *and* as direct children, plus events, metadata and a route.
- An <ActivitySummary> (not stored; must be skipped cleanly).

`imperial=True` writes the same run as 0.5 hr / 3.10686 mi / 1255.2 kJ, so the ground
truth is unchanged and unit normalization is exercised end to end.

All names are synthetic. Manual run: `python tests/fixtures/synthetic.py out.zip 30`.
"""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 1, tzinfo=TZ)
IPHONE = "測試的iPhone"
WATCH = "測試的Apple Watch"

_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [
<!ELEMENT HealthData (ExportDate,Me,(Record|Correlation|Workout|ActivitySummary)*)>
<!ATTLIST HealthData locale CDATA #REQUIRED>
<!ELEMENT ExportDate EMPTY>
<!ATTLIST ExportDate value CDATA #REQUIRED>
]>
<HealthData locale="zh_TW">
 <ExportDate value="2026-11-21 10:00:00 +0800"/>
 <Me HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet"/>
"""


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S +0800")


def _day(w, d: int, imperial: bool) -> None:
    day = BASE + timedelta(days=d)
    for src, dev in ((IPHONE, "iPhone"), (WATCH, "Watch")):
        for h, m in ((7, 30), (12, 0)):
            s = day.replace(hour=h, minute=m)
            w(
                f' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="{src}" sourceVersion="26.1" '
                f'device="&lt;&lt;HKDevice&gt;&gt;, name:{dev}" unit="count" creationDate="{_ts(s)}" '
                f'startDate="{_ts(s)}" endDate="{_ts(s + timedelta(minutes=10))}" value="1000"/>\n'
            )

    s = day.replace(hour=3)
    w(
        f' <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" sourceName="{WATCH}" unit="ms" '
        f'creationDate="{_ts(s)}" startDate="{_ts(s)}" endDate="{_ts(s + timedelta(minutes=1))}" value="45">\n'
        "  <HeartRateVariabilityMetadataList>\n"
        '   <InstantaneousBeatsPerMinute bpm="55" time="3:00:01.00 AM"/>\n'
        "  </HeartRateVariabilityMetadataList>\n"
        " </Record>\n"
    )

    night = day - timedelta(days=1)
    ib = night.replace(hour=22, minute=50)
    w(
        f' <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="{IPHONE}" creationDate="{_ts(ib)}" '
        f'startDate="{_ts(ib)}" endDate="{_ts(day.replace(hour=7, minute=5))}" '
        'value="HKCategoryValueSleepAnalysisInBed"/>\n'
    )
    t = night.replace(hour=23)
    for stage, hrs in (("AsleepCore", 4), ("AsleepDeep", 1), ("AsleepREM", 2)):
        e = t + timedelta(hours=hrs)
        w(
            f' <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="{WATCH}" creationDate="{_ts(e)}" '
            f'startDate="{_ts(t)}" endDate="{_ts(e)}" value="HKCategoryValueSleepAnalysis{stage}">\n'
            '  <MetadataEntry key="HKTimeZone" value="Asia/Taipei"/>\n'
            " </Record>\n"
        )
        t = e
    w(
        f' <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="{WATCH}" creationDate="{_ts(t)}" '
        f'startDate="{_ts(t)}" endDate="{_ts(t + timedelta(minutes=10))}" '
        'value="HKCategoryValueSleepAnalysisAwake"/>\n'
    )

    s = day.replace(hour=6, minute=30)
    e = s + timedelta(minutes=30)
    dur, dur_unit = ("0.5", "hr") if imperial else ("30", "min")
    dist, dist_unit = ("3.10686", "mi") if imperial else ("5", "km")
    kcal, kcal_unit = ("1255.2", "kJ") if imperial else ("300", "kcal")

    def stats(indent: str) -> str:
        return "".join(
            f'{indent}<WorkoutStatistics type="{t_}" startDate="{_ts(s)}" endDate="{_ts(e)}" {kw} unit="{u}"/>\n'
            for t_, u, kw in (
                ("HKQuantityTypeIdentifierDistanceWalkingRunning", dist_unit, f'sum="{dist}"'),
                ("HKQuantityTypeIdentifierActiveEnergyBurned", kcal_unit, f'sum="{kcal}"'),
                ("HKQuantityTypeIdentifierHeartRate", "count/min", 'average="150" minimum="120" maximum="175"'),
            )
        )

    w(
        f' <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="{dur}" durationUnit="{dur_unit}" '
        f'sourceName="{WATCH}" sourceVersion="26.1" creationDate="{_ts(e)}" startDate="{_ts(s)}" endDate="{_ts(e)}">\n'
        '  <MetadataEntry key="HKIndoorWorkout" value="0"/>\n'
        '  <MetadataEntry key="HKTimeZone" value="Asia/Taipei"/>\n'
        f'  <WorkoutEvent type="HKWorkoutEventTypeSegment" date="{_ts(s)}" duration="15" durationUnit="min"/>\n'
        f'  <WorkoutActivity uuid="A{d}" startDate="{_ts(s)}" endDate="{_ts(e)}" duration="{dur}" '
        f'durationUnit="{dur_unit}">\n'
    )
    w(stats("   "))  # iOS 16+: per-activity statistics...
    w("  </WorkoutActivity>\n")
    w(stats("  "))  # ...and the whole-workout ones as direct children
    w(
        f'  <WorkoutRoute sourceName="{WATCH}" creationDate="{_ts(e)}" startDate="{_ts(s)}" endDate="{_ts(e)}">\n'
        f'   <FileReference path="/workout-routes/route_{d}.gpx"/>\n'
        "  </WorkoutRoute>\n"
        " </Workout>\n"
    )

    w(
        f' <ActivitySummary dateComponents="{day:%Y-%m-%d}" activeEnergyBurned="400" activeEnergyBurnedGoal="500" '
        'activeEnergyBurnedUnit="kcal" appleExerciseTime="30" appleExerciseTimeGoal="30" '
        'appleStandHours="10" appleStandHoursGoal="12"/>\n'
    )


def write_xml(stream: io.TextIOBase, n_days: int, *, imperial: bool = False) -> None:
    """Stream the export XML for `n_days` days into a text stream."""
    w = stream.write
    w(_HEADER)
    for d in range(n_days):
        _day(w, d, imperial)
    w("</HealthData>\n")


def write_export(path: Path | str, n_days: int, *, imperial: bool = False) -> Path:
    """Write a zipped export like the iPhone's (`apple_health_export/export.xml`), streamed."""
    path = Path(path)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("apple_health_export/export_cda.xml", "<ClinicalDocument/>")
        with zf.open("apple_health_export/export.xml", "w", force_zip64=True) as raw:
            with io.TextIOWrapper(raw, encoding="utf-8", newline="\n") as text:
                write_xml(text, n_days, imperial=imperial)
    return path


if __name__ == "__main__":
    out, days = sys.argv[1], int(sys.argv[2])
    print(write_export(out, days))
