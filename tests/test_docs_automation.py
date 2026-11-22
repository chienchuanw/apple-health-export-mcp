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


def test_shell_commands_use_the_plists_labels_and_log_paths():
    text = DOC.read_text()
    plists = _plists()
    labels = {p["Label"] for p in plists}
    used = set(re.findall(r"gui/\$\(id -u\)/(local\.apple-health\.[a-z]+)", text))
    assert used and used <= labels
    for p in plists:
        log = p["StandardOutPath"].replace("/Users/you", "~")
        assert log in text, f"no command reads {log}"


# The log lines the guide explains, as printed by the code (cli.py / ingest.py / inbox.py).
_LOG_LINES = [
    "Not ready:",
    "not downloaded yet",
    "still being written",
    "Nothing new:",
    "is already ingested.",
    "Done:",
    "Another ingest is already running; skipping.",
    "the new export has",
]


@pytest.mark.parametrize("line", _LOG_LINES)
def test_explained_log_lines_are_what_the_code_prints(line):
    src = Path(__file__).resolve().parent.parent / "src" / "apple_health_export_mcp"
    code = "".join((src / f).read_text() for f in ("cli.py", "ingest.py", "inbox.py"))
    assert line in DOC.read_text()
    assert line in code


def test_relative_links_resolve():
    for target in re.findall(r"\]\(((?!https?:)[^)#]+)(?:#[^)]*)?\)", DOC.read_text()):
        assert (DOC.parent / target).exists(), target
