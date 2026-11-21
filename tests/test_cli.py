"""The ingest CLI reports what it stored."""

from fixtures.synthetic import write_export

from apple_health_export_mcp.cli import main


def test_cli_reports_workout_statistics(tmp_path, capsys):
    zip_path = write_export(tmp_path / "export.zip", 2)
    assert main([str(zip_path), "--db", str(tmp_path / "health.db")]) == 0
    assert "20 records, 2 workouts (6 workout statistics)" in capsys.readouterr().out
