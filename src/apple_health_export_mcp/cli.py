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
from pathlib import Path

from . import report
from .db import connect_readonly, db_path
from .ingest import ingest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apple-health-export-mcp-ingest",
        description="Parse an Apple Health export.zip into SQLite.",
    )
    parser.add_argument("zip", type=Path, help="Path to the exported export.zip")
    parser.add_argument("--db", type=Path, default=None, help="DB path (default: $AH_DB_PATH or XDG).")
    args = parser.parse_args(argv)

    if not args.zip.expanduser().exists():
        print(f"error: no such file: {args.zip}", file=sys.stderr)
        return 1
    dest = args.db or db_path()
    print(f"Ingesting {args.zip} → {dest}")
    counts = ingest(args.zip, args.db)
    print(
        f"Done: {counts['record']:,} records, {counts['workout']:,} workouts "
        f"({counts['workout_stat']:,} workout statistics) → {dest}"
    )
    return 0


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
