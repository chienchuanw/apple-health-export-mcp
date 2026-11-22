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
import zlib
from contextlib import ExitStack
from pathlib import Path
from xml.etree import ElementTree as ET

from . import inbox, report
from .db import CAN_LOCK, connect_readonly, db_path, writer_lock
from .ingest import ShrinkRefused, ingest, is_ingested

# What a broken or truncated export can raise mid-parse: a bad zip directory or CRC,
# a corrupt deflate stream (zlib.error), a member cut short (EOFError), bad XML.
_EXPORT_ERRORS = (ValueError, zipfile.BadZipFile, zlib.error, EOFError, ET.ParseError, sqlite3.Error, OSError)


def _ingest_one(zip_path: Path, db: Path | None, dest: Path, *, merge: bool, force: bool) -> int:
    print(f"Ingesting {zip_path} → {dest}")
    try:
        counts = ingest(zip_path, db, merge=merge, force=force)
    except ShrinkRefused as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except _EXPORT_ERRORS as e:
        print(f"error: cannot ingest {zip_path}: {e}", file=sys.stderr)
        return 1
    print(
        f"Done: {counts['record']:,} records, {counts['workout']:,} workouts "
        f"({counts['workout_stat']:,} workout statistics) → {dest}"
    )
    return 0


def _ingest_inbox(
    box: Path, db: Path | None, dest: Path, *, merge: bool, force: bool, keep: int | None, settle: float
) -> int:
    try:
        path, why = inbox.pick(box, settle=settle)
        if path is None:
            print(f"Not ready: {why}.")
            return 0
        if is_ingested(path, dest):
            print(f"Nothing new: {path.name} is already ingested.")
        elif rc := _ingest_one(path, db, dest, merge=merge, force=force):
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
        help="With --inbox: seconds a file must stay unchanged to count as copied (default 2).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--merge",
        action="store_true",
        help="Add this export to the existing database instead of rebuilding it "
        "(keeps rows a newer export no longer has).",
    )
    mode.add_argument(
        "--force",
        action="store_true",
        help="Replace the database even if the new export has far fewer records.",
    )
    args = parser.parse_args(argv)
    if (args.zip is None) == (args.inbox is None):
        parser.error("pass either an export.zip or --inbox DIR")
    if args.keep is not None and (args.inbox is None or args.keep < 1):
        parser.error("--keep N needs --inbox and N >= 1")
    if args.settle is not None and (args.inbox is None or args.settle < 0):
        parser.error("--settle S needs --inbox and S >= 0")

    if args.zip is not None and not args.zip.expanduser().exists():
        print(f"error: no such file: {args.zip}", file=sys.stderr)
        return 1
    if not CAN_LOCK:
        print("warning: no file locking on this platform; run one ingest at a time", file=sys.stderr)
    dest = args.db or db_path()
    with ExitStack() as stack:
        try:
            acquired = stack.enter_context(writer_lock(dest))
        except OSError as e:  # e.g. the DB folder cannot be created
            print(f"error: cannot lock {dest}: {e}", file=sys.stderr)
            return 1
        if not acquired:
            if args.inbox:  # a watcher fired again while we were busy: not an error
                print("Another ingest is already running; skipping.")
                return 0
            print("error: another ingest is already running", file=sys.stderr)
            return 1
        if args.inbox:
            settle = 2.0 if args.settle is None else args.settle
            return _ingest_inbox(
                args.inbox.expanduser(),
                args.db,
                dest,
                merge=args.merge,
                force=args.force,
                keep=args.keep,
                settle=settle,
            )
        return _ingest_one(args.zip, args.db, dest, merge=args.merge, force=args.force)


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
