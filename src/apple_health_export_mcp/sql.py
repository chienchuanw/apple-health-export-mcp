"""Opt-in read-only SQL (ADR-0013) — no FastMCP dependency, so it's unit-testable.

`run_select` runs one model-written SELECT against the health DB. The connection is
already `mode=ro`; the authorizer is a second, independent wall that also blocks what
`mode=ro` lets through (ATTACH, PRAGMA, TEMP tables, load_extension). A progress
handler bounds the run time and the result is capped at MAX_ROWS rows.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

MAX_ROWS = 500
TIME_BUDGET_S = 5.0

# Everything a plain SELECT needs, and nothing else. SQLITE_RECURSIVE allows
# WITH RECURSIVE (e.g. a calendar of days); the time budget bounds a runaway one.
# Every other action — writes, ATTACH, PRAGMA (incl. pragma_* table functions),
# CREATE TEMP — is denied.
_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_DENIED_FUNCTIONS = {"load_extension"}

Cell = str | int | float | None


@dataclass
class SqlResult:
    """Rows of a read-only query. `truncated` is true when more than `MAX_ROWS` matched."""

    columns: list[str]
    rows: list[list[Cell]]
    truncated: bool


def _authorize(action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None) -> int:
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _DENIED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def _cell(v: object) -> Cell:
    # BLOB columns (the content-hash keys) are not JSON; hex keeps them joinable by eye.
    return v.hex() if isinstance(v, bytes) else v  # type: ignore[return-value]


def run_select(
    conn: sqlite3.Connection, query: str, *, max_rows: int = MAX_ROWS, budget_s: float = TIME_BUDGET_S
) -> SqlResult:
    """Run one SELECT. Raises ValueError with a model-readable reason on any refusal or SQL error."""
    deadline = time.monotonic() + budget_s
    conn.set_authorizer(_authorize)
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
    try:
        cur = conn.execute(query)
        if cur.description is None:
            raise ValueError("Expected one SELECT statement.")
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.ProgrammingError as e:  # sqlite3 refuses a second statement itself
        raise ValueError(f"{e} Send exactly one SELECT statement.") from e
    except sqlite3.Error as e:
        if str(e) == "interrupted":
            raise ValueError(
                f"Query exceeded the {budget_s:g} s time budget. Narrow it (filter by type and local_date)."
            ) from e
        if "not authorized" in str(e):
            raise ValueError(f"{e}: only a read-only SELECT is allowed (no writes, PRAGMA or ATTACH).") from e
        raise ValueError(f"SQL error: {e}") from e
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
    return SqlResult(
        columns=[d[0] for d in cur.description],
        rows=[[_cell(v) for v in row] for row in rows[:max_rows]],
        truncated=len(rows) > max_rows,
    )
