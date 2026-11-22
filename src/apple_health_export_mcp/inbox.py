"""Drop-folder ingest: pick the newest complete export from a folder (ADR-0015).

For unattended runs (e.g. a launchd `WatchPaths` job on an iCloud Drive folder). A
watcher fires on every change, often several times per copy and while iCloud is still
downloading, so every check here answers "not ready" rather than failing. Whether the
picked file was already ingested is `ingest.is_ingested`. Pure module: no FastMCP import.
"""

from __future__ import annotations

import time
import zipfile
from collections.abc import Callable
from pathlib import Path

_PLACEHOLDER = ".icloud"  # iCloud keeps an evicted "export.zip" as ".export.zip.icloud"


def _is_placeholder(p: Path) -> bool:
    return p.name.startswith(".") and p.name.lower().endswith(".zip" + _PLACEHOLDER)


def _is_zip(p: Path) -> bool:
    """A local export; hidden files (partial copies, placeholders) are not."""
    return not p.name.startswith(".") and p.suffix.lower() == ".zip"


def _newest_first(paths) -> list[Path]:
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def pick(
    box: Path, *, settle: float = 2.0, sleep: Callable[[float], None] = time.sleep
) -> tuple[Path | None, str | None]:
    """Return (newest complete zip, None), or (None, why it is not ready yet).

    The newest of the local zips and the iCloud placeholders wins, so an export that
    is still in the cloud is waited for instead of re-ingesting an older local one.
    A file whose size or mtime changes within `settle` seconds is still being copied.
    """
    candidates = _newest_first(p for p in box.iterdir() if p.is_file() and (_is_zip(p) or _is_placeholder(p)))
    if not candidates:
        return None, f"no export (*.zip) in {box}"
    newest = candidates[0]
    if _is_placeholder(newest):
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


def prune(box: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` zips in `box`; return what was deleted."""
    old = _newest_first(p for p in box.iterdir() if p.is_file() and _is_zip(p))[keep:]
    for p in old:
        p.unlink()
    return old
