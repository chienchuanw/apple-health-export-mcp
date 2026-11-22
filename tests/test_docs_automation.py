"""docs/automation.md: its launchd plists parse and only use flags the CLIs accept."""

import plistlib
import re
from pathlib import Path

import pytest

from apple_health_export_mcp.cli import main, report_main

DOC = Path(__file__).resolve().parent.parent / "docs" / "automation.md"
_XML_BLOCK = re.compile(r"```xml\n(.*?)```", re.S)
_COMMANDS = {"apple-health-export-mcp-ingest": main, "apple-health-export-mcp-report": report_main}


def _plists():
    return [plistlib.loads(block.encode()) for block in _XML_BLOCK.findall(DOC.read_text())]


def _help(entry, capsys) -> str:
    with pytest.raises(SystemExit):
        entry(["--help"])
    return capsys.readouterr().out


def test_the_guide_has_the_two_agents():
    labels = sorted(p["Label"] for p in _plists())
    assert len(labels) == 2 and all(label.startswith("local.apple-health.") for label in labels)


def test_every_plist_runs_a_known_command_with_known_flags(capsys):
    for plist in _plists():
        program, *args = plist["ProgramArguments"]
        command = Path(program).name
        assert command in _COMMANDS, f"{plist['Label']}: unknown program {program}"
        assert Path(program).is_absolute(), "launchd has no user PATH: the program path must be absolute"
        help_text = _help(_COMMANDS[command], capsys)
        for flag in (a for a in args if a.startswith("--")):
            assert flag in help_text, f"{plist['Label']}: {command} has no {flag}"
        assert "AH_DB_PATH" in plist["EnvironmentVariables"]
        assert plist["StandardOutPath"] and plist["StandardErrorPath"]


def test_the_ingest_agent_watches_the_folder_it_ingests():
    (ingest,) = [p for p in _plists() if "ingest" in p["Label"]]
    args = ingest["ProgramArguments"]
    assert ingest["WatchPaths"] == [args[args.index("--inbox") + 1]]


def test_the_report_agent_runs_weekly():
    (report,) = [p for p in _plists() if "report" in p["Label"]]
    assert set(report["StartCalendarInterval"]) >= {"Weekday", "Hour", "Minute"}
