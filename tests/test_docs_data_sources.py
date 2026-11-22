"""docs/data-sources.md and ADR-0018: the hands-free source evaluation (#17)."""

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
DOC = DOCS / "data-sources.md"


def _adr(number: str) -> str:
    text = (DOCS / "adr.md").read_text()
    match = re.search(rf"^## ADR-{number} .*?(?=^## ADR-|\Z)", text, re.S | re.M)
    assert match, f"no ADR-{number}"
    return match.group(0)


def test_adr_0018_is_proposed_and_covers_the_adrs_a_new_source_touches():
    adr = _adr("0018")
    # Pinned on purpose: whoever accepts or supersedes ADR-0018 (#24) updates this line.
    assert "**Status:** proposed" in adr
    for dependency in ("0004", "0005", "0009", "0010", "0011", "0012", "0015", "0016"):
        assert f"ADR-{dependency}" in adr, f"ADR-0018 says nothing about ADR-{dependency}"
    assert "data-sources.md" in adr


@pytest.mark.parametrize("question", range(1, 8))
def test_each_of_the_seven_questions_has_an_entry_for_both_candidates(question):
    text = DOC.read_text()
    shortcuts, hae = text.split("## A) ")[1].split("## B) ")
    hae = hae.split("\n## ")[0]
    for section in (shortcuts, hae):
        assert re.search(rf"^{question}\. \*\*", section, re.M), f"question {question} missing"


def test_the_evaluation_says_it_was_not_device_tested():
    assert "Nothing here was tested on a device" in DOC.read_text()


def test_relative_links_resolve():
    for target in re.findall(r"\]\(((?!https?:)[^)#]+)(?:#[^)]*)?\)", DOC.read_text()):
        assert (DOC.parent / target).exists(), target


def test_readme_and_automation_guide_link_the_evaluation():
    for page in (DOCS.parent / "README.md", DOCS / "automation.md"):
        targets = re.findall(r"\]\(([^)#]*data-sources\.md)\)", page.read_text())
        assert targets, f"{page.name} does not link data-sources.md"
        assert all((page.parent / target).resolve() == DOC for target in targets)


def test_open_device_tests_are_tracked():
    assert "issues/24" in DOC.read_text()
    assert "#24" in _adr("0018")
