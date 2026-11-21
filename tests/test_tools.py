"""Server-side tool surface: read-only DB access and clean errors."""

import sqlite3
import zipfile

import pytest
from fastmcp.exceptions import ToolError

from apple_health_export_mcp import tools
from apple_health_export_mcp.ingest import ingest

XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" startDate="2026-06-18 08:00:00 +0300" endDate="2026-06-18 08:05:00 +0300" value="900"/>
</HealthData>
"""


@pytest.fixture
def ingested_db(tmp_path, monkeypatch):
    """An ingested DB at a path with a space and URI-special characters."""
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("apple_health_export/export.xml", XML)
    db = tmp_path / "my data #1?" / "health 100%.db"
    ingest(zip_path, db, progress=lambda *_: None)
    monkeypatch.setenv("AH_DB_PATH", str(db))
    return db


def test_tools_read_through_the_server_connection(ingested_db):
    types = tools.list_types()
    assert [(t.type, t.n) for t in types] == [("HKQuantityTypeIdentifierStepCount", 1)]


def test_server_connection_cannot_write(ingested_db):
    with tools._store() as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO meta(k,v) VALUES('x','y')")


def test_missing_db_names_the_real_ingest_command(tmp_path, monkeypatch):
    monkeypatch.setenv("AH_DB_PATH", str(tmp_path / "absent.db"))
    with pytest.raises(ToolError, match="apple-health-export-mcp-ingest <export.zip>"):
        tools.list_types()


def _drop_sidecars(db):
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            side.unlink()


def test_ingest_leaves_a_single_file_database(ingested_db):
    """A read-only reader can't create -wal/-shm, so ingest must not leave the DB in WAL mode."""
    conn = sqlite3.connect(ingested_db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
    _drop_sidecars(ingested_db)  # Linux SQLite deletes them on close; macOS keeps them
    assert [t.type for t in tools.list_types()] == ["HKQuantityTypeIdentifierStepCount"]


def test_wal_database_without_sidecars_stays_readable(ingested_db):
    """A DB built by 0.1.1 is persistently in WAL mode; it must still open read-only."""
    conn = sqlite3.connect(ingested_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    _drop_sidecars(ingested_db)
    assert [t.type for t in tools.list_types()] == ["HKQuantityTypeIdentifierStepCount"]
    with tools._store() as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO meta(k,v) VALUES('x','y')")


def test_database_errors_surface_as_tool_errors(tmp_path, monkeypatch):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()  # valid SQLite file, but no schema
    monkeypatch.setenv("AH_DB_PATH", str(empty))
    with pytest.raises(ToolError, match="no such table"):
        tools.list_types()
