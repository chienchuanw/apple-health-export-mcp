"""Server-side tool surface: read-only DB access and clean errors."""

import sqlite3
import zipfile

import pytest
from fastmcp.exceptions import ToolError

from apple_health_export_mcp import tools
from apple_health_export_mcp.db import connect_readonly, remove_db_files
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
    remove_db_files(db, main=False)


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


_CRASHING_WRITER = r"""
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("PRAGMA cache_size=1")  # force uncommitted pages to spill into the DB file
c.execute("PRAGMA cache_spill=1")
c.execute("BEGIN EXCLUSIVE")
c.execute("UPDATE t SET v = 'DIRTY' || substr(v, 6)")
c.executemany("INSERT INTO t(v) VALUES (?)", [("x" * 500,)] * 20000)
os._exit(0)  # crash mid-transaction: leaves a hot rollback journal
"""


def test_crashed_writer_is_never_read_as_committed_data(tmp_path):
    """A hot journal must surface as an error, not be bypassed by the immutable fallback."""
    import subprocess
    import sys

    db = tmp_path / "health.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t(v) VALUES (?)", [("clean" + "y" * 400,)] * 2000)
    conn.commit()
    conn.close()
    subprocess.run([sys.executable, "-c", _CRASHING_WRITER, str(db)], check=True)
    assert db.with_name(db.name + "-journal").exists()

    try:
        conn = connect_readonly(db)
        dirty = conn.execute("SELECT COUNT(*) FROM t WHERE v LIKE 'DIRTY%'").fetchone()[0]
        conn.close()
    except sqlite3.OperationalError:
        return  # refusing to read is correct
    assert dirty == 0, "read uncommitted pages from a crashed transaction"


def test_reingest_with_an_open_reader_still_succeeds(tmp_path, monkeypatch):
    """A server connection opened mid-ingest (WAL active) must not fail a committed ingest."""
    from fixtures.synthetic import write_export

    from apple_health_export_mcp import ingest as ingest_mod

    zip_path = write_export(tmp_path / "export.zip", 3)
    db = tmp_path / "health.db"
    ingest(zip_path, db, progress=lambda *_: None)
    monkeypatch.setattr(ingest_mod, "BATCH", 5)  # commit early so a reader can join mid-ingest
    held = []

    def progress(msg):
        if "records" in msg and not held:
            held.append(connect_readonly(db))
            held[0].execute("SELECT COUNT(*) FROM record").fetchall()

    try:
        counts = ingest(zip_path, db, progress=progress)
    finally:
        for c in held:
            c.close()
    assert held and counts["record"] == 30
    monkeypatch.setenv("AH_DB_PATH", str(db))
    assert len(tools.list_types()) == 4  # still readable, whatever journal mode it ended in


@pytest.mark.parametrize(("merge", "written"), [(True, "health.db"), (False, "health.db.building")])
def test_wal_file_exists_for_the_whole_ingest(tmp_path, merge, written):
    """connect_readonly treats "no -wal" as "no writer"; ingest must create it before parsing.

    A merge writes the live DB; a rebuild writes `<db>.building` and leaves the live DB
    untouched (no -wal: correctly "no writer") until the swap.
    """
    from fixtures.synthetic import write_export

    zip_path = write_export(tmp_path / "export.zip", 1)
    db = tmp_path / "health.db"
    ingest(zip_path, db, progress=lambda *_: None)  # first build ends in DELETE mode: no -wal
    seen = []

    def progress(msg):
        if msg.startswith("Reading"):
            seen.append((tmp_path / f"{written}-wal").exists())

    ingest(zip_path, db, progress=progress, merge=merge)
    assert seen == [True]
