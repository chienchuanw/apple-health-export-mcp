"""Coaching prompt workflows — reusable templates the client can invoke.

Plain (un-decorated) functions so this module stays free of the FastMCP instance;
`server.py` registers each via `mcp.prompt`. Written in English; every prompt tells
the model to reply in the user's language. Date arguments are optional and default
to today / this week / this month / this year, so each prompt works with no input.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import Field

from apple_health_export_mcp.report import week_range

# Shared tail appended to every prompt: tone, language, and output contract.
_REPLY = (
    "Reply in the user's language. Be concise and concrete — cite the actual numbers. "
    "Call out anything notable (trends, anomalies, over-training or under-recovery signals) "
    "and end with one actionable recommendation. If a metric has no data, say so briefly."
)

_DayArg = Annotated[str, Field(description="Date 'YYYY-MM-DD'; defaults to today.")]


def _today() -> str:
    return date.today().isoformat()


def daily_summary(day: _DayArg = "") -> str:
    """A single day's health snapshot: steps, sleep, resting heart rate, workouts, energy."""
    day = day or _today()
    return (
        f"Summarize my health on {day}. Use the apple-health tools: get_quantity for steps "
        f"(HKQuantityTypeIdentifierStepCount, sum), active energy "
        f"(HKQuantityTypeIdentifierActiveEnergyBurned, sum) and resting heart rate "
        f"(HKQuantityTypeIdentifierRestingHeartRate, avg); get_sleep for the night ending {day}; "
        f"get_workouts for any session that day. Give a short snapshot of how the day looked. {_REPLY}"
    )


def weekly_review(
    week_of: Annotated[str, Field(description="Any date inside the wanted week; defaults to this week.")] = "",
) -> str:
    """Calendar-week review (Mon–Sun): training load, sleep, recovery, and a recommendation."""
    start, end = week_range(week_of or _today())
    return (
        f"Review the calendar week {start} to {end} (Monday–Sunday). Use get_workouts for training "
        f"load, get_sleep for nightly sleep, get_quantity for daily steps "
        f"(HKQuantityTypeIdentifierStepCount, sum) and resting heart rate "
        f"(HKQuantityTypeIdentifierRestingHeartRate, avg, bucket=day). Assess whether recovery is "
        f"keeping up with load and whether to push or pull back volume next week. {_REPLY}"
    )


def monthly_summary(
    month: Annotated[str, Field(description="Month 'YYYY-MM'; defaults to this month.")] = "",
) -> str:
    """A month in review: totals, averages, body-weight change, and trend."""
    month = month or _today()[:7]
    return (
        f"Summarize the month {month}. Use get_quantity (bucket=week) for steps "
        f"(HKQuantityTypeIdentifierStepCount, sum), resting heart rate "
        f"(HKQuantityTypeIdentifierRestingHeartRate, avg) and body weight "
        f"(HKQuantityTypeIdentifierBodyMass, avg); get_workouts and get_sleep across the month. "
        f"Report totals/averages, how body weight changed, and the "
        f"overall trend versus a healthy month. {_REPLY}"
    )


def yearly_summary(
    year: Annotated[str, Field(description="Year 'YYYY'; defaults to this year.")] = "",
) -> str:
    """A year in review: big-picture fitness trajectory and milestones."""
    year = year or _today()[:4]
    return (
        f"Give a year-in-review for {year}. Use get_quantity (bucket=month) for body weight "
        f"(HKQuantityTypeIdentifierBodyMass, avg), resting heart rate "
        f"(HKQuantityTypeIdentifierRestingHeartRate, avg) and VO2Max "
        f"(HKQuantityTypeIdentifierVO2Max, avg, if present), plus monthly steps "
        f"(HKQuantityTypeIdentifierStepCount, sum); get_workouts for total training. "
        f"Describe the fitness trajectory over the year and the biggest changes. {_REPLY}"
    )


def readiness_check() -> str:
    """Should I train hard today? Based on last night's sleep and recent recovery markers."""
    return (
        "Assess whether I'm ready to train hard today. Use get_sleep for last night, and "
        "get_quantity for resting heart rate (HKQuantityTypeIdentifierRestingHeartRate, avg, "
        "bucket=day) and HRV (HKQuantityTypeIdentifierHeartRateVariabilitySDNN, avg, bucket=day) "
        "over the last ~10 days to judge recovery. "
        "Compare last night and today's markers to my recent baseline, then recommend: full "
        f"intensity, moderate, or rest. {_REPLY}"
    )


def sleep_report(start_date: _DayArg = "", end_date: _DayArg = "") -> str:
    """Sleep deep-dive over a range: duration, stage breakdown, and consistency."""
    end_date = end_date or _today()
    start_date = start_date or end_date
    return (
        f"Analyze my sleep from {start_date} to {end_date} using get_sleep. Report average time "
        f"asleep, REM/deep/core breakdown, and how consistent bedtimes/durations were. Flag short "
        f"or fragmented nights and suggest one improvement. {_REPLY}"
    )


# Registered by server.py via mcp.prompt(fn).
ALL = [
    daily_summary,
    weekly_review,
    monthly_summary,
    yearly_summary,
    readiness_check,
    sleep_report,
]
