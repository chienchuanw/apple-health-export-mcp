"""The ingest CLI reports what it stored."""

import zipfile

from fixtures.synthetic import write_export

from apple_health_export_mcp.cli import main


def test_cli_reports_workout_statistics(tmp_path, capsys):
    zip_path = write_export(tmp_path / "export.zip", 2)
    assert main([str(zip_path), "--db", str(tmp_path / "health.db")]) == 0
    assert "20 records, 2 workouts (6 workout statistics)" in capsys.readouterr().out


def test_cli_unreadable_export_is_a_one_line_error(tmp_path, capsys):
    bad = tmp_path / "export.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("readme.txt", "no health data here")
    assert main([str(bad), "--db", str(tmp_path / "health.db")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "HealthData" in err and "Traceback" not in err
