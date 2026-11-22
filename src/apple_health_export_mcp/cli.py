"""Command-line entry points: ingest (ADR-0006) and the weekly report.

`apple-health-export-mcp-ingest` builds the SQLite DB from an Apple Health
export.zip; `apple-health-export-mcp-report` writes a weekly Markdown note from it.
Both are separate from the server, which is the bare `apple-health-export-mcp`
command (see server.main). Run from PyPI/git with:
`uvx --from apple-health-export-mcp apple-health-export-mcp-ingest <zip>`.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from . import inbox, report
from .db import CAN_LOCK, connect_readonly, db_path, writer_lock
from .ingest import ingest, is_ingested


def _ingest_one(zip_path: Path, db: Path | None, dest: Path, digest: str | None = None) -> int:
    print(f"Ingesting {zip_path} → {dest}")
    try:
        counts = ingest(zip_path, db, digest=digest)
    except (ValueError, zipfile.BadZipFile, ET.ParseError, sqlite3.Error, OSError) as e:
        print(f"error: cannot ingest {zip_path}: {e}", file=sys.stderr)
        return 1
    print(
        f"Done: {counts['record']:,} records, {counts['workout']:,} workouts "
        f"({counts['workout_stat']:,} workout statistics) → {dest}"
    )
    return 0


def _ingest_inbox(box: Path, db: Path | None, dest: Path, keep: int | None, settle: float) -> int:
    try:
        path, why = inbox.pick(box, settle=settle)
        if path is None:
            print(f"Not ready: {why}.")
            return 0
        done, digest = is_ingested(path, dest)
        if done:
            print(f"Nothing new: {path.name} is already ingested.")
        elif rc := _ingest_one(path, db, dest, digest):
            return rc  # a failed ingest prunes nothing
        if keep:
            for old in inbox.prune(box, keep):
                print(f"Removed older export {old.name}")
    except (OSError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apple-health-export-mcp-ingest",
        description="Parse an Apple Health export.zip into SQLite.",
    )
    parser.add_argument("zip", type=Path, nargs="?", help="Path to the exported export.zip")
    parser.add_argument("--db", type=Path, default=None, help="DB path (default: $AH_DB_PATH or XDG).")
    parser.add_argument(
        "--inbox",
        type=Path,
        help="Drop folder instead of a zip: ingest its newest complete *.zip unless already ingested.",
    )
    parser.add_argument("--keep", type=int, help="With --inbox: after success, delete all but the newest N zips.")
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="With --inbox: seconds a file must stay unchanged to count as copied (default 2).",
    )
    args = parser.parse_args(argv)
    if (args.zip is None) == (args.inbox is None):
        parser.error("pass either an export.zip or --inbox DIR")
    if args.keep is not None and (args.inbox is None or args.keep < 1):
        parser.error("--keep N needs --inbox and N >= 1")

    if args.zip is not None and not args.zip.expanduser().exists():
        print(f"error: no such file: {args.zip}", file=sys.stderr)
        return 1
    if not CAN_LOCK:
        print("warning: no file locking on this platform; run one ingest at a time", file=sys.stderr)
    dest = args.db or db_path()
    with writer_lock(dest) as acquired:
        if not acquired:
            if args.inbox:  # a watcher fired again while we were busy: not an error
                print("Another ingest is already running; skipping.")
                return 0
            print("error: another ingest is already running", file=sys.stderr)
            return 1
        if args.inbox:
            return _ingest_inbox(args.inbox.expanduser(), args.db, dest, args.keep, args.settle)
        return _ingest_one(args.zip, args.db, dest)


def report_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apple-health-export-mcp-report",
        description="Write a weekly Markdown health report (e.g. into an Obsidian vault).",
    )
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--week", help="ISO week 'YYYY-Www' (e.g. 2026-W47).")
    when.add_argument("--week-of", help="Any date 'YYYY-MM-DD' inside the wanted week.")
    # Neither: the last completed Monday-Sunday week.
    parser.add_argument(
        "--out",
        type=Path,
        default=os.environ.get("AH_REPORT_DIR") or None,  # empty = unset
        help="Output folder (default: $AH_REPORT_DIR).",
    )
    parser.add_argument("--db", type=Path, default=None, help="DB path (default: $AH_DB_PATH or XDG).")
    args = parser.parse_args(argv)

    if args.out is None:
        print("error: no output folder: pass --out or set AH_REPORT_DIR", file=sys.stderr)
        return 1
    try:
        label, start, end = report.resolve_week(week=args.week, week_of=args.week_of)
        conn = connect_readonly(args.db)
    except (ValueError, FileNotFoundError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        text = report.build(conn, label, start, end)
    except sqlite3.Error as e:
        print(f"error: database error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    try:
        note = report.write(args.out.expanduser(), label, text)
    except OSError as e:
        print(f"error: cannot write the report: {e}", file=sys.stderr)
        return 1
    print(f"Wrote {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
