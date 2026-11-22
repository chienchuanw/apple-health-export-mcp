# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `apple-health-export-mcp-report` writes a weekly Markdown note (YAML frontmatter
  with the week's numbers, per-day and per-workout tables) for a notes vault such as
  Obsidian. Deterministic, read-only, and a rerun on unchanged data leaves the file
  untouched. The note is owner-only (`0600`) (ADR-0014).
- `apple-health-export-mcp-ingest --inbox DIR [--keep N] [--settle S]` ingests the newest complete
  `*.zip` in a drop folder, and does nothing when it is not downloaded or copied yet or
  was already ingested. Safe to call repeatedly from a file watcher (ADR-0015).

- `get_baseline` tool: a metric's recent mean against your own preceding baseline
  (delta, delta %, z-score), including sleep as minutes asleep per night.
  `get_training_load` tool: weekly workout volume with an acute:chronic ratio.
  `readiness_check` and `weekly_review` now use them (ADR-0017).

- `docs/automation.md`: a weekly macOS pipeline with two launchd agents, one that
  ingests from an iCloud Drive inbox and one that writes the weekly report into a
  notes vault. It covers where files should live and the iCloud/TCC pitfalls.

- `docs/data-sources.md`: an evaluation of hands-free alternatives to the manual export
  (iOS Shortcuts, Health Auto Export), from their documentation. Neither replaces
  `export.xml` for now: iOS locks Health data with the phone, and Shortcuts cannot read
  workouts. Recorded as ADR-0018 (proposed), with the device tests that would reopen it.

### Changed
- Re-ingest now rebuilds the database from the export and swaps it in, so entries
  edited or deleted in the Health app no longer linger or double-count. It refuses
  (exit 1) to replace a database with one that has under 90% of its records, or one
  it can't read, unless `--force` is given. `--merge` keeps the old add-to-existing behaviour. Needs about
  twice the database size in free disk space while it runs (ADR-0016).
- Every successful ingest records the zip's SHA-256 in `meta`, and ingest holds a
  `<db>.lock` file lock, so two ingests never write the same database at once.
- An unreadable export (no health data, corrupt XML) now exits 1 with a one-line
  `error:` instead of a traceback.

## [0.3.0] - 2026-11-22

### Added
- Opt-in `query_sql` tool (`AH_ENABLE_SQL=1`): one read-only `SELECT` per call for
  questions the fixed tools can't answer. An SQLite authorizer allows only reads
  (no writes, `PRAGMA`, `ATTACH`, `load_extension`), queries stop after 5 s and
  results are capped at 500 rows (with a `truncated` flag) and 100 KB per value.
  Off by default (ADR-0013).

## [0.2.0] - 2026-11-21

### Added
- `get_workouts` now also returns `total_distance_km`, `total_energy_kcal` and a
  duration-weighted `avg_heart_rate`, parsed from `<WorkoutStatistics>` (direct or
  nested in `<WorkoutActivity>`) with a fallback to the legacy `totalDistance` /
  `totalEnergyBurned` attributes. Stored in a new `workout_stat` table; re-run ingest
  to populate it for an existing database (ADR-0011).
- A workout without a heart-rate statistic takes `avg_heart_rate` from the
  `HeartRate` samples recorded during it, when there is at least one per minute
  of its start-to-end window (ADR-0012). Applies to existing databases without
  re-ingest.
- `get_workouts` reports `max_min`, the longest single workout per activity, so a
  session left running is visible instead of hiding in `total_min` / `avg_min`.
- Synthetic export fixture with known ground truth (`tests/fixtures/synthetic.py`)
  and end-to-end tests for dedupe, sleep, workouts and idempotent re-ingest.

### Changed
- Dependency bound tightened to `fastmcp>=3.4.10,<4` (stay on the 3.x major); lockfile refreshed.
- CI actions are pinned to full commit SHAs; CI runs with `contents: read` and does
  not persist the checkout token.
- Ingest still uses WAL for speed but leaves the database as a single file
  (`journal_mode=DELETE`), and its summary reports the workout statistics count.
- A malformed numeric attribute is stored as null instead of aborting the ingest.

### Fixed
- `get_workouts` `total_min` / `avg_min` honor `durationUnit` (hours and seconds were
  summed as minutes). Applies to existing databases without re-ingest.
- Coaching prompts named metrics by short names (`StepCount`, `RestingHeartRate`…),
  which `get_quantity` never matches; they now use full `HKQuantityTypeIdentifier…` ids.
- The missing-database hint named `apple-health-export-mcp ingest` instead of the real
  `apple-health-export-mcp-ingest` command.
- Database errors raised while a tool runs surface as a clean tool error.

### Security
- The health database (and its `-wal` / `-shm` files) is created owner-only (`0600`),
  a directory it creates is `0700`, and an existing database is tightened on ingest.
- The MCP server opens the database read-only (SQLite `mode=ro` via a
  percent-encoded `file:` URI); only ingest writes. A 0.1.1-built database (left in
  WAL mode) stays readable even when its `-wal`/`-shm` files are gone, and
  `get_workouts` works on it before re-ingest (statistics read as null until then).
- The sdist is built from an explicit allowlist, so a local `uv build` can no longer
  pick up untracked files (notes, session logs, health exports) from the checkout.

## [0.1.1] - 2026-06-30

### Changed
- Tools now return typed dataclass models instead of bare dicts, so each tool
  publishes a JSON **output schema** (`structuredContent` is unchanged). Clients
  can validate fields and see the shape up front.
- Internal: extracted the tool surface into `tools.py` (mirrors `prompts.py`), so
  `server.py` is wiring only. No behavior change.

## [0.1.0]

### Added
- Streaming ingest of an Apple Health `export.zip` into SQLite (`apple-health-export-mcp ingest`),
  locale-independent (finds the main XML by content, not filename).
- Idempotent, content-hash de-duplication on re-ingest.
- Two console scripts: `apple-health-export-mcp` (bare command runs the stdio MCP
  server) and `apple-health-export-mcp-ingest` (build the DB). The server shuts down
  on stdin EOF and is meant to be launched directly (no wrapper) to avoid orphaned
  processes — see README "Shutdown & process model".
- MCP tools: `list_types`, `list_sources`, `get_quantity`, `get_sleep`, `get_workouts`
  (aggregates only; configurable date range).
- Per-day dominant-source de-duplication for `sum` aggregation, mirroring Apple's
  multi-source merge so totals aren't inflated.
- Coaching prompts: `daily_summary`, `weekly_review`, `monthly_summary`,
  `yearly_summary`, `readiness_check`, `sleep_report`.

[Unreleased]: https://github.com/chienchuanw/apple-health-export-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/chienchuanw/apple-health-export-mcp/tree/v0.3.0
[0.2.0]: https://github.com/chienchuanw/apple-health-export-mcp/tree/v0.2.0
[0.1.1]: https://github.com/burakdirin/apple-health-export-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/burakdirin/apple-health-export-mcp/releases/tag/v0.1.0
