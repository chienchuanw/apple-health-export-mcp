"""SQLite store: schema + connection. See docs/adr.md (ADR-0002, 0004, 0009)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Apple writes timestamps in local time with the offset appended
# ("2026-06-18 23:13:35 +0300"), so the local calendar day is just the
# first 10 chars — no timezone conversion needed (ADR-0009).
SCHEMA = """
CREATE TABLE IF NOT EXISTS record (
    key        BLOB PRIMARY KEY,   -- content hash, idempotent (ADR-0005)
    type       TEXT NOT NULL,      -- HK*TypeIdentifier*, locale-independent
    source     TEXT,               -- sourceName, may be localized/personal
    unit       TEXT,
    value      TEXT,               -- TEXT: quantity=number, category=enum string
    start      TEXT NOT NULL,      -- raw, with offset
    end        TEXT,
    created    TEXT,
    local_date TEXT NOT NULL       -- substr(start,1,10): local day, for bucketing
);
CREATE INDEX IF NOT EXISTS ix_record_type_date ON record(type, local_date);

CREATE TABLE IF NOT EXISTS workout (
    key          BLOB PRIMARY KEY,
    activity     TEXT NOT NULL,    -- workoutActivityType
    duration     REAL,
    duration_unit TEXT,
    source       TEXT,
    start        TEXT NOT NULL,
    end          TEXT,
    created      TEXT,
    local_date   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_workout_date ON workout(local_date);

-- Per-workout statistics (distance, energy, heart rate…), units kept raw (ADR-0011).
-- Legacy exports without <WorkoutStatistics> store the Workout's totalDistance /
-- totalEnergyBurned attributes here under those attribute names as `type`.
CREATE TABLE IF NOT EXISTS workout_stat (
    workout_key BLOB NOT NULL,     -- workout.key
    type        TEXT NOT NULL,     -- HKQuantityTypeIdentifier*, or totalDistance/totalEnergyBurned
    unit        TEXT,
    sum         REAL,
    average     REAL,
    minimum     REAL,
    maximum     REAL,
    start       TEXT,
    end         TEXT,
    PRIMARY KEY (workout_key, type)
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def db_path() -> Path:
    """Resolve the DB path from config/env (ADR-0007). No hard-coded location."""
    env = os.environ.get("AH_DB_PATH")
    if env:
        return Path(env).expanduser()
    # Fallback: XDG data dir.
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "apple-health-export-mcp" / "health.db"


def _make_private(p: Path) -> None:
    """Create the DB file owner-only (0600) and tighten an existing one.

    Health data must not be world-readable. Only the directory we create is set to
    0700 — a pre-existing parent (e.g. ~/Documents via AH_DB_PATH) is left alone.
    SQLite creates -wal/-shm with the main file's mode, but tighten any stale ones too.
    """
    if not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)  # exist_ok: a concurrent ingest may win the race
        os.chmod(p.parent, 0o700)  # explicit: mkdir's mode is filtered by the umask
    os.close(os.open(p, os.O_CREAT | os.O_RDWR, 0o600))
    for f in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
        if f.exists():
            os.chmod(f, 0o600)


def _require(p: Path) -> None:
    if not p.exists():
        raise FileNotFoundError(
            f"No database at {p}. Run `apple-health-export-mcp-ingest <export.zip>` first "
            f"(set AH_DB_PATH to control the location)."
        )


def connect(path: Path | None = None, *, create: bool = True) -> sqlite3.Connection:
    """Read-write connection — for ingest. The server uses `connect_readonly`."""
    p = path or db_path()
    if create:
        _make_private(p)
    else:
        _require(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    return conn


def connect_readonly(path: Path | None = None) -> sqlite3.Connection:
    """Read-only connection (SQLite `mode=ro`) — every MCP tool goes through this.

    The URI is built by pathlib (`as_uri()` percent-encodes spaces, `#`, `?`, `%`),
    so an arbitrary DB path cannot inject extra URI parameters.
    """
    p = (path or db_path()).resolve()
    _require(p)
    uri = p.as_uri()
    conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.OperationalError:
        # A WAL-mode DB (e.g. built by 0.1.1) whose -wal/-shm are gone — deleted on
        # close, or the file was copied alone. A mode=ro reader can't recreate them.
        # No WAL file means no writer and nothing un-checkpointed, so immutable is safe.
        conn.close()
        if p.with_name(p.name + "-wal").exists():
            raise
        conn = sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
