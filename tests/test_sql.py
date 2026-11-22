"""Opt-in read-only SQL tool (ADR-0013): only one SELECT, bounded in time and rows."""

import asyncio
import importlib
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fixtures.synthetic import write_export

from apple_health_export_mcp import server, sql, tools
from apple_health_export_mcp.db import connect, connect_readonly
from apple_health_export_mcp.ingest import ingest

STEPS = "HKQuantityTypeIdentifierStepCount"
COUNT_TO = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < {}) SELECT n FROM r"


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sql")
    path = tmp / "health.db"
    ingest(write_export(tmp / "export.zip", 3), path, progress=lambda *_: None)
    return path


@pytest.fixture
def ro(db):
    conn = connect_readonly(db)
    yield conn
    conn.close()


def test_select_returns_columns_and_rows(ro):
    out = sql.run_select(ro, f"SELECT type, COUNT(*) AS n FROM record WHERE type = '{STEPS}' GROUP BY type")
    assert out.columns == ["type", "n"]
    assert out.rows == [[STEPS, ro.execute("SELECT COUNT(*) FROM record WHERE type=?", (STEPS,)).fetchone()[0]]]
    assert out.truncated is False


def test_schema_is_discoverable_through_sqlite_master(ro):
    out = sql.run_select(ro, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    assert ["record"] in out.rows and ["workout_stat"] in out.rows


def test_results_are_capped_and_truncation_is_reported(ro):
    full = sql.run_select(ro, COUNT_TO.format(sql.MAX_ROWS))
    assert len(full.rows) == sql.MAX_ROWS and full.truncated is False
    over = sql.run_select(ro, COUNT_TO.format(sql.MAX_ROWS + 100))
    assert len(over.rows) == sql.MAX_ROWS and over.truncated is True
    assert over.rows[-1] == [sql.MAX_ROWS]


def test_blobs_come_back_as_hex(ro):
    assert sql.run_select(ro, "SELECT x'00ff', 1.5, NULL").rows == [["00ff", 1.5, None]]


def test_long_running_query_is_aborted(ro):
    endless = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r"
    with pytest.raises(ValueError, match="time budget"):
        sql.run_select(ro, endless, budget_s=0.2)
    assert ro.execute("SELECT 1").fetchone()[0] == 1  # connection still usable


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO meta(k, v) VALUES ('x', 'y')",
        "UPDATE record SET value = '0'",
        "DELETE FROM record",
        "CREATE TEMP TABLE t(a)",  # allowed even on a mode=ro connection without the authorizer
        "DROP TABLE record",
        "PRAGMA table_info(record)",
        "PRAGMA journal_mode = WAL",
        "SELECT * FROM pragma_table_info('record')",
    ],
)
def test_anything_but_select_is_denied(db, stmt):
    """Uses a read-write connection: the authorizer alone must stop it, not mode=ro."""
    conn = connect(db, create=False)
    try:
        before = conn.execute("SELECT COUNT(*) FROM record").fetchone()[0]
        with pytest.raises(ValueError, match="not authorized"):  # refused before it runs
            sql.run_select(conn, stmt)
        assert conn.execute("SELECT COUNT(*) FROM record").fetchone()[0] == before
    finally:
        conn.close()


def test_attach_is_denied_and_creates_no_file(ro, tmp_path):
    target = tmp_path / "other.db"
    with pytest.raises(ValueError, match="not authorized"):
        sql.run_select(ro, f"ATTACH DATABASE '{target}' AS other")
    assert not target.exists()


def test_load_extension_is_denied_by_the_authorizer(ro):
    """Python's sqlite3 disables it (some builds lack it entirely); the authorizer denies it regardless."""
    with pytest.raises(ValueError):
        sql.run_select(ro, "SELECT load_extension('anything')")
    assert sql._authorize(sqlite3.SQLITE_FUNCTION, None, "load_extension", None, None) == sqlite3.SQLITE_DENY
    assert sql._authorize(sqlite3.SQLITE_FUNCTION, None, "LOAD_EXTENSION", None, None) == sqlite3.SQLITE_DENY
    assert sql._authorize(sqlite3.SQLITE_FUNCTION, None, "count", None, None) == sqlite3.SQLITE_OK


@pytest.mark.parametrize("stmt", ["SELECT 1; SELECT 2", "SELECT 1; DELETE FROM record"])
def test_multiple_statements_are_rejected(ro, stmt):
    with pytest.raises(ValueError, match="one statement"):
        sql.run_select(ro, stmt)


@pytest.mark.parametrize("stmt", ["", "  ", "-- just a comment"])
def test_empty_input_is_rejected(ro, stmt):
    with pytest.raises(ValueError, match="SELECT"):
        sql.run_select(ro, stmt)


def test_tool_reads_through_the_server_connection(db, monkeypatch):
    monkeypatch.setenv("AH_DB_PATH", str(db))
    out = tools.query_sql("SELECT COUNT(DISTINCT local_date) AS days FROM record")
    assert out.columns == ["days"] and out.rows[0][0] >= 3


def test_tool_reports_denied_queries_as_tool_errors(db, monkeypatch):
    monkeypatch.setenv("AH_DB_PATH", str(db))
    with pytest.raises(ToolError, match="not authorized"):
        tools.query_sql("DELETE FROM record")


def _registered_tools(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AH_ENABLE_SQL", raising=False)
    else:
        monkeypatch.setenv("AH_ENABLE_SQL", value)
    mod = importlib.reload(server)

    async def names():
        async with Client(mod.mcp) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(names())


@pytest.fixture
def reload_server_after(monkeypatch):
    yield
    monkeypatch.undo()
    importlib.reload(server)  # leave the module as a default (SQL-less) server for other tests


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_sql_tool_is_off_unless_enabled(monkeypatch, reload_server_after, value):
    names = _registered_tools(monkeypatch, value)
    assert "query_sql" not in names and "get_quantity" in names


def test_sql_tool_is_registered_with_ah_enable_sql_1(monkeypatch, reload_server_after):
    assert "query_sql" in _registered_tools(monkeypatch, "1")


def test_tool_description_states_the_real_limits():
    doc = tools.query_sql.__doc__
    assert f"after {sql.TIME_BUDGET_S:g} s" in doc and "AH_ENABLE_SQL=1" in doc


@pytest.mark.parametrize(
    "stmt",
    [
        "SELECT zeroblob(300000000)",
        "SELECT randomblob(300000000)",
        "SELECT group_concat(printf('%.*c', 1000, 'x')) FROM (" + COUNT_TO.format(1000) + ")",
    ],
)
def test_huge_values_are_refused_instead_of_allocated(ro, stmt):
    """One function call can build a huge value in a few VM steps, past the time budget."""
    with pytest.raises(ValueError, match="too big"):
        sql.run_select(ro, stmt)
    assert len(ro.execute("SELECT printf('%.*c', 1000000, 'x')").fetchone()[0]) == 1_000_000  # limit restored


def test_oversized_printf_yields_null(ro):
    """SQLite's printf returns NULL past SQLITE_LIMIT_LENGTH instead of allocating."""
    assert sql.run_select(ro, "SELECT printf('%.*c', 300000000, 'x')").rows == [[None]]
    assert len(ro.execute("SELECT printf('%.*c', 1000000, 'x')").fetchone()[0]) == 1_000_000  # limit restored


def test_parameter_placeholders_get_their_own_error(ro):
    with pytest.raises(ValueError) as e:
        sql.run_select(ro, "SELECT * FROM record WHERE type = ?")
    assert "one SELECT statement" not in str(e.value) and "inline" in str(e.value)


def test_every_authorizer_denial_explains_the_read_only_rule(ro, tmp_path):
    target = tmp_path / "x.db"
    with pytest.raises(ValueError, match="only a read-only SELECT"):
        sql.run_select(ro, f"VACUUM INTO '{target}'")
    assert not target.exists()


def test_non_finite_floats_stay_valid_json(ro):
    assert sql.run_select(ro, "SELECT 1e999, -1e999").rows == [["inf", "-inf"]]


def test_tool_description_lists_every_column():
    from apple_health_export_mcp.db import SCHEMA

    doc = tools.query_sql.__doc__
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for table in ("record", "workout", "workout_stat", "meta"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert f"{table}({', '.join(cols)})" in doc
