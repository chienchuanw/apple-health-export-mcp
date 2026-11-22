"""Drop-folder ingest: pick the newest complete export, skip one already ingested.

For unattended runs (e.g. a launchd `WatchPaths` job on an iCloud Drive folder). A
watcher fires on every change, often several times per copy and while iCloud is still
downloading, so every check here answers "not ready" rather than failing. Pure module:
no FastMCP import.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .db import connect_readonly, private_dir

try:
    import fcntl
except ImportError:  # Windows: no flock
    fcntl = None

_PLACEHOLDER = ".icloud"  # iCloud keeps an evicted "export.zip" as ".export.zip.icloud"


def _zips(box: Path) -> list[Path]:
    """Local exports in `box`, newest first. Hidden files (partial copies) are skipped."""
    found = [p for p in box.iterdir() if p.is_file() and not p.name.startswith(".") and p.suffix.lower() == ".zip"]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def pick(
    box: Path, *, settle: float = 2.0, sleep: Callable[[float], None] = time.sleep
) -> tuple[Path | None, str | None]:
    """Return (newest complete zip, None), or (None, why it is not ready yet).

    The newest of the local zips and the iCloud placeholders wins, so an export that
    is still in the cloud is waited for instead of re-ingesting an older local one.
    A file whose size or mtime changes within `settle` seconds is still being copied.
    """
    placeholders = [
        p for p in box.iterdir() if p.name.startswith(".") and p.name.lower().endswith(".zip" + _PLACEHOLDER)
    ]
    candidates = _zips(box) + placeholders
    if not candidates:
        return None, f"no export (*.zip) in {box}"
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if newest in placeholders:
        return None, f"{newest.name[1 : -len(_PLACEHOLDER)]} is in iCloud but not downloaded yet"
    before = newest.stat()
    if settle:
        sleep(settle)
        after = newest.stat()
        if (after.st_size, after.st_mtime) != (before.st_size, before.st_mtime):
            return None, f"{newest.name} is still being written"
    if not zipfile.is_zipfile(newest):
        return None, f"{newest.name} is not a complete zip yet"
    return newest, None


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file, read in 1 MB chunks (an export can be gigabytes)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def ingested_sha256(db: Path) -> str | None:
    """Digest of the zip the last *successful* ingest read, or None."""
    if not db.exists():
        return None
    conn = connect_readonly(db)
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='source_sha256'").fetchone()
    except sqlite3.OperationalError:  # a DB older than the meta table
        return None
    finally:
        conn.close()
    return row["v"] if row else None


def prune(box: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` zips in `box`; return what was deleted."""
    old = _zips(box)[keep:]
    for p in old:
        p.unlink()
    return old


@contextmanager
def lock(db: Path) -> Iterator[bool]:
    """Hold an exclusive, non-blocking lock on `<db>.lock`; yields False if it is taken.

    Two ingests writing the same DB would interleave batches; a watcher can start a
    second run while the first is still parsing.
    """
    if fcntl is None:
        print("warning: no file locking on this platform; run one ingest at a time", file=sys.stderr)
        yield True
        return
    private_dir(db.parent)
    with open(db.with_name(db.name + ".lock"), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
