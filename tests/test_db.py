"""Storage hardening: the health DB must be private to the owner (0600, dir 0700)."""

import os
import stat
from pathlib import Path

from apple_health_export_mcp.db import connect, init_schema


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_new_db_and_directory_are_owner_only(tmp_path):
    db = tmp_path / "nested" / "health.db"
    conn = connect(db, create=True)
    init_schema(conn)
    conn.close()

    assert _mode(db) == 0o600
    assert _mode(db.parent) == 0o700


def test_existing_db_permissions_are_tightened(tmp_path):
    db = tmp_path / "health.db"
    db.touch()
    os.chmod(db, 0o644)

    connect(db, create=True).close()

    assert _mode(db) == 0o600


def test_wal_and_shm_sidecars_are_owner_only(tmp_path):
    db = tmp_path / "health.db"
    old = os.umask(0o022)  # the common default, which would make new files world-readable
    try:
        conn = connect(db, create=True)
        conn.execute("PRAGMA journal_mode=WAL")
        init_schema(conn)
        conn.execute("INSERT INTO meta(k,v) VALUES('x','y')")
        conn.commit()
        for side in ("health.db-wal", "health.db-shm"):
            assert _mode(tmp_path / side) == 0o600, side
        conn.close()
    finally:
        os.umask(old)


def test_directory_created_concurrently_is_not_an_error(tmp_path, monkeypatch):
    """Two first-run ingests race between the exists() check and mkdir()."""
    parent = tmp_path / "data"
    parent.mkdir()
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if self == parent else real_exists(self))
    connect(parent / "health.db", create=True).close()
    assert _mode(parent / "health.db") == 0o600


def test_stale_journal_is_tightened_to_owner_only(tmp_path):
    db = tmp_path / "health.db"
    db.touch(mode=0o644)
    journal = tmp_path / "health.db-journal"
    journal.touch(mode=0o644)
    os.chmod(journal, 0o644)
    connect(db, create=True).close()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
