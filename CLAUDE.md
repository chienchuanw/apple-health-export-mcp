# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastMCP (v3, `>=3.4.10,<4`) stdio MCP server that answers questions about a user's **exported** Apple Health archive. Apple has no live API, so the flow is two-stage and snapshot-based:

```
export.zip ──(ingest CLI, one-time)──► SQLite (health.db) ──◄── MCP server tools query it
```

The design rationale lives in [`docs/adr.md`](docs/adr.md) (12 ADRs). **Read it before changing how data is parsed, stored, or aggregated, and add an ADR entry for such changes** — the reasoning there is load-bearing and not re-derivable from the diff.

## Commands

```bash
uv sync                                   # set up the env
uv run pytest                             # all tests
uv run pytest tests/test_queries.py::test_sum_dedupes_parallel_sources_by_dominant_per_day  # single test
uv run ruff check                         # lint (CI gate)
uv run ruff format                        # format (CI checks --check)
uv run fastmcp inspect                    # list registered tools/prompts without a client
uv build                                  # sdist+wheel; uvx twine check dist/* before publishing
uv run python tests/fixtures/synthetic.py /tmp/big.zip 150000   # manual ingest benchmark input (not in CI)

# Run the pieces directly:
apple-health-export-mcp-ingest <export.zip>   # build the DB (AH_DB_PATH or --db controls location)
apple-health-export-mcp                        # run the stdio server (reads the DB)
```

## Architecture

Layering is deliberate — keep it:

- **`queries.py`** — pure SQL helpers + the typed result dataclasses (`TypeInfo`, `QuantityPoint`, `SleepNight`, …), **no FastMCP import**. All query logic and the unit tests live here so it stays testable without a server.
- **`sql.py`** — the opt-in `query_sql` engine (authorizer allowlist, time budget, row cap, ADR-0013); also **no FastMCP import**.
- **`tools.py`** — the MCP tool surface: thin wrappers over `queries.py`, the `_store()` context manager (read-only DB open/close + `ValueError`/missing-DB → `ToolError`), and the reusable `Annotated[..., Field(...)]` argument types. Plain (un-decorated) functions exported via `ALL`.
- **`prompts.py`** — coaching prompt **content** as plain functions exported via `ALL`.
- **`server.py`** — wiring only: creates the `mcp` instance, registers `tools.enabled()` (`tools.ALL`, plus `query_sql` when opted in) (`mcp.tool(annotations=READONLY)`) and `prompts.ALL` (`mcp.prompt`), and holds `main()` + shutdown. `tools.py`/`prompts.py` stay decorator-free so they never import the `mcp` instance (no circular import); returning typed models lets FastMCP emit an output schema per tool.
- **`ingest.py`** — streaming `iterparse` of the export XML into SQLite; the one-time builder.
- **`db.py`** — schema + `connect()` + `db_path()` (env/XDG resolution).
- **`report.py`** — the weekly Markdown report (`build` / `write` / `resolve_week`), built on `queries.py`; **no FastMCP import**.
- **`cli.py`** — the `apple-health-export-mcp-ingest` and `apple-health-export-mcp-report` entry points.

Three console scripts (see `[project.scripts]`): the **bare** `apple-health-export-mcp` runs the server (`server:main`), `-ingest` builds the DB and `-report` writes the weekly note. This Pattern-B split is intentional (matches reference MCP servers; shallow process tree).

## Non-obvious invariants (these will bite you)

- **`server.py` uses absolute imports** (`from apple_health_export_mcp import ...`), not relative. `fastmcp run` loads the file by path with no package parent, so relative imports break there.
- **Generic schema, `type` is a string column.** New HealthKit types need no code. `record.value` is **TEXT** (quantity types are numeric, category types are string enums like `…AsleepDeep`); numeric SQL must `CAST(value AS REAL)`.
- **Local day = `substr(start, 1, 10)`.** Apple writes timestamps in local time with the offset appended (`2026-06-29 08:14:22 +0300`), stored verbatim — no timezone conversion needed or wanted (ADR-0009).
- **`get_quantity` `sum` de-duplicates by per-day dominant source** (most records that day) to avoid double-counting parallel devices (Watch + iPhone + apps); `avg/min/max/count` are left raw. The known ceiling (mid-day device switch can under-count) and the upgrade path are in ADR-0010.
- **The main export XML is found by content, not filename** — a localized export names it e.g. `dışa aktarılan.xml`, not `export.xml`. Type identifiers and sleep enums stay English regardless of `locale`.
- **Tools return aggregates only** (ADR-0008) — never raw rows; a metric can hold millions of samples. The one exception is the opt-in `query_sql` (`sql.py`, registered by `tools.enabled()` only when `AH_ENABLE_SQL=1`, ADR-0013): its `set_authorizer` allowlist is the security boundary, since `mode=ro` still permits `ATTACH`/`PRAGMA`/`CREATE TEMP`. Don't widen the allowlist, and keep the limits in the `query_sql` docstring in sync with `sql.py` (a test checks).
- **Server is read-only, ingest is the only writer.** Tools go through `db.connect_readonly()` (`mode=ro`, URI built with `Path.as_uri()` — never hand-format the path into the URI); `db.connect(create=True)` creates the DB `0600` (directory `0700`). Don't route a tool through `connect()`. Ingest uses WAL for speed and ends with a best-effort `journal_mode=DELETE` (a `mode=ro` reader can't create `-wal`/`-shm`; the switch is refused while a reader is attached). `connect_readonly` falls back to `immutable=1` **only** on `SQLITE_CANTOPEN` with no `-wal` file (a WAL DB whose sidecars are gone, e.g. 0.1.1-built). Never widen that: on a lock or a hot journal, immutable reads uncommitted pages. Ingest commits right after switching to WAL so a `-wal` file exists for its whole run ("no `-wal`" must keep meaning "no writer").
- **Workout statistics keep raw units** in `workout_stat`; `queries.py` normalizes in SQL via the `_TO_MIN` / `_TO_KM` / `_TO_KCAL` tables. An unknown unit must map to NULL, not a guessed factor. Legacy exports store `totalDistance` / `totalEnergyBurned` as `type`, only when no matching statistic exists (ADR-0011). Parse a `Workout`'s children **before** `elem.clear()`. A workout without an HR statistic falls back to `HeartRate` samples inside its window, only at >= 1 sample per minute of the start-to-end window (`win_min`, rounded: a raw `julianday` difference is a float, so 30 min reads 30.0000002), matched as UTC instants via `queries._utc` (ADR-0012).
- **Prompts must name metrics by full identifier** (`HKQuantityTypeIdentifierStepCount`), written as `(<type>, <agg>…)` — `tests/test_prompts.py` parses that pattern.
- **Ground truth lives in `tests/fixtures/synthetic.py`** (docstring lists the expected per-day numbers). Keep every name in it synthetic — this is a public repo.
- **Shutdown:** the server exits on stdin EOF (the MCP spec's primary mechanism) and installs a SIGINT handler that `os._exit(0)`s to avoid hanging on the blocking stdin-reader thread. Launch it with as few wrapper layers as possible (`uv`/`uvx` only conditionally forward signals → orphan risk). Never reintroduce `fastmcp run fastmcp.json` as the client-facing command.

## Paths & config

No hard-coded paths. The zip is a CLI argument; the DB path comes from `AH_DB_PATH` (else XDG default `~/.local/share/apple-health-export-mcp/health.db`). `.gitignore` excludes `*.zip`/`*.db` so health data never enters git.

## Release

Version is single-sourced from `src/apple_health_export_mcp/__init__.py` (`[tool.hatch.version]`). To release: bump `__version__`, update `CHANGELOG.md`, then `gh release create vX.Y.Z` — `.github/workflows/publish.yml` publishes to PyPI via Trusted Publishing (OIDC, no token). CI (`ci.yml`) runs ruff + pytest on 3.11–3.13.
