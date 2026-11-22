"""Prompt content must name metrics by their full HealthKit identifier.

`get_quantity` matches `type` exactly, so a short name like `StepCount` returns
no data. Prompts write a metric as `(<type>, <agg>...)`; check every such type.
"""

import re

from apple_health_export_mcp import prompts

# Capitalized first token inside parentheses, then ',' or ')': "(StepCount, sum)".
_TYPE_IN_PARENS = re.compile(r"\(([A-Z][A-Za-z0-9]+)[,)]")
# Every metric the prompts ask for — guards the regex against silently matching nothing.
_EXPECTED = {
    "StepCount",
    "ActiveEnergyBurned",
    "RestingHeartRate",
    "BodyMass",
    "VO2Max",
    "HeartRateVariabilitySDNN",
    "HKCategoryTypeIdentifierSleepAnalysis",  # get_baseline's sleep series
}


def test_every_prompt_type_is_a_full_healthkit_identifier():
    seen = set()
    for fn in prompts.ALL:
        text = fn()
        found = set(_TYPE_IN_PARENS.findall(text))
        if "get_quantity" in text or "get_baseline" in text:
            assert found, f"{fn.__name__} calls get_quantity/get_baseline but names no type"
        for t in found:
            assert t.startswith("HK"), f"{fn.__name__}: {t!r} is not a full HealthKit identifier"
        seen |= found
    assert {t.removeprefix("HKQuantityTypeIdentifier") for t in seen} == _EXPECTED
