"""Weekly Markdown report: week resolution, numbers vs synthetic ground truth, CLI."""

import ast
import os
import sqlite3
import stat
from datetime import date
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from fixtures.synthetic import write_export

from apple_health_export_mcp import report
from apple_health_export_mcp.cli import report_main
from apple_health_export_mcp.db import connect_readonly, init_schema
from apple_health_export_mcp.ingest import ingest


def test_week_of_resolves_to_its_monday_to_sunday_week():
    assert report.resolve_week(week_of="2026-10-01") == ("2026-W40", "2026-09-28", "2026-10-04")


def test_iso_week_resolves_to_its_dates():
    assert report.resolve_week(week="2026-W47") == ("2026-W47", "2026-11-16", "2026-11-22")


def test_iso_week_label_uses_the_iso_year_at_a_year_boundary():
    # 2027-01-01 is a Friday, so its week belongs to ISO year 2026 (week 53).
    assert report.resolve_week(week_of="2027-01-01") == ("2026-W53", "2026-12-28", "2027-01-03")


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 11, 23), "2026-W47"),  # Monday: last week just completed
        (date(2026, 11, 22), "2026-W46"),  # Sunday: this week is not over yet
        (date(2026, 11, 18), "2026-W46"),  # midweek
    ],
)
def test_default_week_is_the_last_completed_week(today, expected):
    assert report.resolve_week(today=today)[0] == expected


@pytest.mark.parametrize("bad", ["2026-47", "2026-W00", "2026-W54", "W47"])
def test_malformed_iso_week_is_rejected(bad):
    with pytest.raises(ValueError, match="YYYY-Www"):
        report.resolve_week(week=bad)


def test_week_and_week_of_are_exclusive():
    with pytest.raises(ValueError, match="not both"):
        report.resolve_week(week="2026-W40", week_of="2026-10-01")


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory):
    """4 synthetic days (2026-10-01..04), all inside ISO week 2026-W40."""
    tmp = tmp_path_factory.mktemp("report")
    db = tmp / "health.db"
    ingest(write_export(tmp / "export.zip", 4), db, progress=lambda *_: None)
    return db


def _build(db, week_of="2026-10-01"):
    conn = connect_readonly(db)
    try:
        return report.build(conn, *report.resolve_week(week_of=week_of))
    finally:
        conn.close()


def _frontmatter(md: str) -> dict[str, str]:
    assert md.startswith("---\n")
    head = md[4 : md.index("\n---\n", 4)]
    return dict(line.split(": ", 1) for line in head.splitlines())


def test_frontmatter_matches_synthetic_ground_truth(synth_db):
    fm = _frontmatter(_build(synth_db))
    assert fm == {
        "week": "2026-W40",
        "start": "2026-09-28",
        "end": "2026-10-04",
        # iPhone + Watch each log 2000/day; deduped to one device -> 2000/day, not 4000.
        "steps_total": "8000",
        "steps_daily_avg": "2000",
        "active_kcal_total": "null",  # no ActiveEnergyBurned records in the fixture
        "resting_hr_avg": "null",  # no RestingHeartRate records either
        "hrv_avg": "45",
        "sleep_asleep_avg_min": "420",
        "sleep_nights": "4",
        "workouts_count": "4",
        "workout_min_total": "120",
        "workout_km_total": "20",
        "workout_kcal_total": "1200",
    }


def test_body_lists_every_day_workouts_and_missing_metrics(synth_db):
    md = _build(synth_db)
    assert "| 2026-09-28 Mon | – | – | – | – | – |" in md  # before the data starts
    assert "| 2026-10-01 Thu | 2000 | – | – | 45 | 420 |" in md
    assert "| Running | 4 | 120 | 30 | 20 | 1200 | 150 |" in md
    assert "No data this week: active energy, resting heart rate." in md


def test_empty_week_reports_no_data_instead_of_zeros(synth_db):
    md = _build(synth_db, week_of="2026-11-18")
    fm = _frontmatter(md)
    assert fm["steps_total"] == "null" and fm["workouts_count"] == "0" and fm["sleep_nights"] == "0"
    assert "No workouts this week." in md


def test_build_is_deterministic(synth_db):
    assert _build(synth_db) == _build(synth_db)


def test_report_module_does_not_import_fastmcp():
    tree = ast.parse(Path(report.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.split(".")[0] == "fastmcp" for m in imported)


def test_cli_writes_the_week_note_into_out(synth_db, tmp_path):
    out = tmp_path / "vault" / "Health"  # created if missing
    rc = report_main(["--week-of", "2026-10-01", "--out", str(out), "--db", str(synth_db)])
    assert rc == 0
    assert (out / "2026-W40.md").read_text() == _build(synth_db)


def test_cli_takes_out_from_env(synth_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AH_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("AH_DB_PATH", str(synth_db))
    assert report_main(["--week", "2026-W40"]) == 0
    assert (tmp_path / "2026-W40.md").exists()


def test_cli_rerun_on_unchanged_data_leaves_the_file_untouched(synth_db, tmp_path):
    args = ["--week-of", "2026-10-01", "--out", str(tmp_path), "--db", str(synth_db)]
    assert report_main(args) == 0
    note = tmp_path / "2026-W40.md"
    os.utime(note, (1, 1))  # an old mtime: a rewrite would bump it
    assert report_main(args) == 0
    assert note.stat().st_mtime == 1


def test_cli_without_out_fails_cleanly(synth_db, monkeypatch, capsys):
    monkeypatch.delenv("AH_REPORT_DIR", raising=False)
    assert report_main(["--db", str(synth_db)]) == 1
    assert "AH_REPORT_DIR" in capsys.readouterr().err


def test_cli_missing_db_fails_cleanly(tmp_path, capsys):
    assert report_main(["--out", str(tmp_path), "--db", str(tmp_path / "nope.db")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err
    assert not list(tmp_path.glob("*.md"))


def test_cli_bad_week_fails_cleanly(synth_db, tmp_path, capsys):
    assert report_main(["--week", "2026-47", "--out", str(tmp_path), "--db", str(synth_db)]) == 1
    assert "YYYY-Www" in capsys.readouterr().err


def test_report_console_script_is_registered():
    eps = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert eps["apple-health-export-mcp-report"] == "apple_health_export_mcp.cli:report_main"


def test_weekly_resting_hr_is_the_sample_mean_not_the_mean_of_daily_means():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    rhr = "HKQuantityTypeIdentifierRestingHeartRate"
    for i, (start, v) in enumerate(
        [("2026-10-05 07:00:00 +0800", "50"), ("2026-10-05 19:00:00 +0800", "60"), ("2026-10-06 07:00:00 +0800", "70")]
    ):
        conn.execute(
            "INSERT INTO record(key,type,value,start,end,local_date) VALUES (?,?,?,?,?,?)",
            (bytes([i]), rhr, v, start, start, start[:10]),
        )
    fm = _frontmatter(report.build(conn, *report.resolve_week(week_of="2026-10-05")))
    assert fm["resting_hr_avg"] == "60"  # (50+60+70)/3, not (55+70)/2 = 62.5


def test_cli_treats_an_empty_report_dir_as_unset(synth_db, monkeypatch, capsys):
    monkeypatch.setenv("AH_REPORT_DIR", "")
    assert report_main(["--db", str(synth_db)]) == 1
    assert "AH_REPORT_DIR" in capsys.readouterr().err


def test_note_is_owner_only_like_the_db(synth_db, tmp_path):
    assert report_main(["--week-of", "2026-10-01", "--out", str(tmp_path), "--db", str(synth_db)]) == 0
    assert stat.S_IMODE((tmp_path / "2026-W40.md").stat().st_mode) == 0o600


def test_cli_out_that_is_a_file_fails_cleanly(synth_db, tmp_path, capsys):
    not_a_dir = tmp_path / "taken"
    not_a_dir.write_text("x")
    assert report_main(["--out", str(not_a_dir), "--db", str(synth_db)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "Traceback" not in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_cli_unwritable_out_fails_cleanly_and_leaves_no_temp_file(synth_db, tmp_path, capsys):
    out = tmp_path / "ro"
    out.mkdir()
    out.chmod(0o500)
    try:
        assert report_main(["--week-of", "2026-10-01", "--out", str(out), "--db", str(synth_db)]) == 1
        assert capsys.readouterr().err.startswith("error:")
        assert list(out.iterdir()) == []
    finally:
        out.chmod(0o700)


@pytest.mark.parametrize("flag", ["--week", "--week-of"])
def test_cli_empty_week_argument_is_an_error_not_last_week(synth_db, tmp_path, capsys, flag):
    assert report_main([flag, "", "--out", str(tmp_path), "--db", str(synth_db)]) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert not list(tmp_path.glob("*.md"))
