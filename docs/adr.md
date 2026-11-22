# Architecture Decision Record (log)

One entry per decision. Newest at bottom. Status: `accepted` unless noted.

---

## ADR-0001 — Snapshot ingest, no live API
**Status:** accepted
**Context:** Apple Health has no public/off-device export API; the only way out is the user manually running *Export All Health Data*, which produces a full `export.zip`. There is no incremental/delta export.
**Decision:** The MCP is snapshot-based. The user exports manually and points the tool at the zip. Each export is a *complete* re-dump of all history.
**Consequence:** Data is never "live"; freshness == last manual export. Re-export = full re-ingest (see ADR-0005). Documented as a known limitation, not a bug.

---

## ADR-0002 — SQLite as the query store
**Status:** accepted
**Context:** `export.xml` can be 100 MB–1 GB+. Querying the XML on every request is slow and memory-heavy.
**Decision:** Ingest parses the XML once into a local SQLite DB. All MCP tools query SQLite, never the XML.
**Consequence:** Sub-millisecond queries after a one-time ~1–3 min ingest. SQLite is stdlib — zero added dependency for storage.

---

## ADR-0003 — Streaming parse (iterparse)
**Status:** accepted
**Context:** The XML is too large to load as a DOM (OOM risk).
**Decision:** Parse with `xml.etree.ElementTree.iterparse`, calling `elem.clear()` per element to free memory. Batch inserts in transactions (~10k). `PRAGMA journal_mode=WAL; synchronous=OFF` *during* ingest only.
**Consequence:** Flat memory profile regardless of file size. Bottleneck is I/O + parse, not insert. Forgetting `clear()` is the one failure mode → covered by a self-check.

---

## ADR-0004 — Generic record schema (type as a string column)
**Status:** accepted
**Context:** Apple has hundreds of `HK*TypeIdentifier` types (steps, heart rate, sleep, body mass, …) and adds more over time. All `<Record>` elements share one flat shape.
**Decision:** Single `record(type, source, start, end, value, unit, …)` table keyed on `type` as a TEXT column — no per-type schema. `<Workout>` and `<Correlation>` are separate elements → their own tables. Type discovery is a query: `SELECT DISTINCT type`.
**Consequence:** New Apple types appear automatically, no code change. `value` is TEXT (Category types carry string enums like `…AsleepDeep`); numeric queries `CAST(value AS REAL)`.

---

## ADR-0005 — Idempotent ingest via content hash
**Status:** accepted
**Context:** Records have no stable UUID in the export, and every export is a full snapshot. Re-ingesting must not duplicate rows.
**Decision:** Synthetic key = hash(type, source, start, end, value, unit), `UNIQUE`, `INSERT OR IGNORE`.
**Consequence:** Re-ingesting the same or a newer export only adds the new tail; DB accumulates across exports and keeps old rows even if a later export drops them. Re-ingest still full-scans (~2 min) because Apple gives no delta. Hash collision is theoretically possible but negligible across the field tuple. <!-- ponytail: if a collision ever shows, add row-offset to the key -->

---

## ADR-0006 — Ingest is a separate CLI command, not an MCP tool
**Status:** accepted
**Context:** Ingest takes ~1–3 min. Running it inside the stdio MCP server would block the client and risk timeouts.
**Decision:** `apple-health-export-mcp ingest <export.zip>` (or `uv run …`) builds/updates the DB. The MCP server only ever reads a ready DB.
**Consequence:** Clean separation, no timeout/progress machinery in the server. Setup is a two-step story (ingest once, then run server) — documented in README.

---

## ADR-0007 — Paths are config-driven (fastmcp.json / env)
**Status:** accepted
**Context:** Standalone public repo; every user has their own zip and wants the DB wherever suits them.
**Decision:** No hard-coded paths. Zip path is the ingest CLI argument. DB path comes from config/env (e.g. `AH_DB_PATH`), surfaced via `fastmcp.json` environment interpolation. Personal data never enters the repo.
**Consequence:** Portable across machines. `.gitignore` excludes `*.zip` and `*.db` as a privacy backstop.

---

## ADR-0008 — Tools return aggregations only
**Status:** accepted
**Context:** A metric can hold millions of raw rows — far past any LLM context window, and a token bomb.
**Decision:** Tools return aggregated summaries (sum/avg/min/max per day/week; sleep = summed stage durations). A date range is **required**. No raw-row dump.
**Consequence:** Low, predictable token cost; output is decision-useful, not a data dump. If raw access is ever needed it's a deliberate later addition. <!-- ponytail: raw=true escape hatch only if a real need appears -->

---

## ADR-0009 — Store timestamps raw, with offset
**Status:** accepted
**Context:** Apple emits `2026-06-29 08:14:22 +0300`. Sleep/steps are sensitive to the *local* day boundary; normalizing to UTC corrupts "which local day" across DST/travel.
**Decision:** Store the timestamp string verbatim, offset included. "Local day" queries use the stored offset.
**Consequence:** No data loss, travel/DST-correct. Slightly more care in date-range SQL (compare on local date derived from the stored value).

---

## ADR-0010 — De-duplicate `sum` by per-day dominant source
**Status:** accepted
**Context:** The export holds raw, un-merged samples from every source. The same activity is logged in parallel by multiple devices (real data: 7 step sources — Apple Watch, iPhone, HUAWEI Sağlık, Zepp…), so a naive `SUM` over all rows over-counts (~40% on this user's steps). Apple itself never sums raw samples: the Health app keeps a per-type **user-ordered source priority** and `HKStatisticsQuery` merges overlapping samples by picking the higher-priority source per interval (WWDC 2014 s203 / 2016 s209). That priority list is **not** in the export, and there is no offline statistics API. `HKMetadataKeySyncIdentifier` only dedups the *same* logical sample re-synced by one app — not parallel-device overlap. Community parsers (qs_ledger, healthkit-to-sqlite) either store raw (push the bug to queries) or filter to one global source (loses other eras / wear patterns).
**Decision:** Exact-duplicate rows are already collapsed at ingest by the content-hash key (ADR-0005). For `sum` aggregation, de-duplicate by picking, **per local day, the single dominant source** (most records that day = the device actually in use) and summing only it. `avg/min/max/count` are left raw (extra samples don't inflate them). A `source` argument forces one source (the `separateBySource` escape hatch); `list_sources(type)` exposes the per-source breakdown for transparency.
**Consequence:** Default `sum` is no longer inflated, with zero user configuration. Era-robust (pre-Watch days pick whatever recorded) and locale-proof (ranks by record count, not fragile source-name matching). **Known ceiling:** on a day split across devices mid-day (each covering part of it) the single-source pick can *under*-count. The exact fix is per-interval union with a priority rank — the documented upgrade path, deferred until single-source undercounting is shown to matter. No mainstream offline parser ships that.

---

## ADR-0011 — Workout statistics in a side table, units normalized at query time
**Status:** accepted
**Context:** `get_workouts` only reported count and duration, although the export carries per-workout distance, energy and heart rate. Where they live depends on the export era: direct `<WorkoutStatistics>` children of `<Workout>` (whole-workout values); iOS 16+ may also or only nest them per segment inside `<WorkoutActivity>`; older exports have no statistics at all, just `totalDistance`/`totalEnergyBurned` attributes on `<Workout>`. Units vary (`km`/`mi`/`m`/`yd`, `kcal`/`Cal`/`kJ`), and `durationUnit` was ignored — `SUM(duration)` added hours and seconds as if they were minutes.
**Decision:**
- New table `workout_stat(workout_key, type, unit, sum, average, minimum, maximum, start, end)`, `PRIMARY KEY (workout_key, type)`, created with `CREATE TABLE IF NOT EXISTS` and filled with `INSERT OR IGNORE` — idempotent (ADR-0005), and an existing DB upgrades by re-running ingest (the `workout` rows are skipped, their statistics are added). A side table, not new `workout` columns, because `CREATE TABLE IF NOT EXISTS` cannot add columns to an existing table.
- Parsed on the `Workout` end event, before `elem.clear()`. Precedence per `type`: a direct child wins; otherwise nested segments are merged (sums add, min/max of extremes, average weighted by segment time). The legacy attributes are stored under their attribute names as `type` (`totalDistance`, `totalEnergyBurned`) **only** when no matching statistic exists in that `<Workout>`. Because ingest is additive, a later export can still add a statistic for a workout first stored from legacy attributes, so `get_workouts` also applies the precedence per workout at query time (`COALESCE(statistic, legacy attribute)`) — nothing is counted twice.
- Units stay raw in the DB (as ADR-0009 does for timestamps) and are normalized in SQL: distance → km, energy → kcal, duration → minutes (a missing `durationUnit` means minutes, Apple's default). The duration fix therefore applies to existing DBs without re-ingest. An **unknown unit maps to NULL** and drops out of the total rather than producing a plausible but wrong number.
- `get_workouts` adds `total_distance_km` (all `HKQuantityTypeIdentifierDistance*` types, so running, cycling and swimming all count), `total_energy_kcal` (active energy) and `avg_heart_rate` (each workout's average weighted by its duration, over workouts that have one). It also reports `max_min`, the longest single workout: a session left running inflates `total_min`/`avg_min`, and surfacing it beats excluding it behind an arbitrary cutoff. Still aggregates only (ADR-0008).
**Consequence:** Distance/energy/HR questions are answerable per activity and range, across export eras. Merged segments that disagree on a type's unit store a NULL unit (so a NULL total, never a mixed-unit sum), and segment spans are ordered by actual time, not by offset-bearing text. Malformed numbers parse as NULL rather than aborting ingest. Known ceiling: a workout without heart-rate data does not dilute `avg_heart_rate` but also contributes nothing to it (narrowed by ADR-0012). <!-- ponytail: per-segment rows only if a real need appears -->

## ADR-0012 — Heart-rate fallback from samples recorded during a workout
**Status:** accepted
**Context:** Some apps record a workout without writing a `HKQuantityTypeIdentifierHeartRate` statistic, so ADR-0011 leaves its `avg_heart_rate` empty even when the Watch logged a dense `HeartRate` trace during the session. Other sessions only overlap the Watch's background readings, a few per session, which say little about the effort.
**Decision:**
- At query time, a workout with no HR statistic takes the mean of the `HeartRate` records whose `start` falls inside its `[start, end]`. The statistic always wins when present. No new table and no re-ingest: the samples are already stored.
- The fallback applies only with **at least one sample per minute** of the workout's start-to-end window, the span the samples are drawn from. Active `duration` would let a paused workout pass with sparser samples. If the duration is longer than the window (a truncated or zero-length window), the duration is used instead, so a single reading can never set the value. Sparser readings leave it null, in the same spirit as an unknown unit mapping to NULL: no plausible but rough number.
- Instants are compared in UTC (`queries._utc` rewrites `+0300` as `+03:00` for SQLite's `julianday`), never as text, because a sample may carry another offset. `local_date` only bounds the `(type, local_date)` index scan, widened by a day on each side for offset changes and workouts crossing midnight.
**Consequence:** Workouts recorded by such apps gain a heart rate when the Watch traced them. The mean is per sample, not time-weighted, which matches a near-uniform workout trace. Parallel HR sources are not deduplicated (as for `avg` in ADR-0010): the mean blends them by sample count, and two sources that are each too sparse can pass the threshold together. <!-- ponytail: dominant-source pick per workout if blended means mislead in practice --> A full-range `get_workouts` stays well under a second on typical Watch data; the scan grows with HR sample density, so a device writing HR at ~1 Hz all day makes a multi-year query take seconds.

## ADR-0013 — Opt-in read-only SQL tool
**Status:** accepted
**Context:** The fixed tools answer the common questions, but cross-metric or unusual ones ("steps on days I ran vs days I did not", "resting HR the morning after a long run") have no path, and a purpose-built tool per question does not scale. Letting the model run its own SQL is the general answer, but it returns rows rather than aggregates, which goes against ADR-0008, and it puts model-written SQL next to private data.
**Decision:**
- New tool `query_sql(sql)`, registered **only when `AH_ENABLE_SQL=1`** (exactly `1`; anything else, or unset, leaves it off). The default surface stays aggregates-only, so ADR-0008 holds unless the user opts in.
- Logic in `sql.py` (no FastMCP import, like `queries.py`); the tool goes through `_store()`, so the connection is `mode=ro` as for every tool (ADR-0006 / server is read-only).
- `mode=ro` alone is not enough: it still allows `ATTACH` (which can open or create another file), `PRAGMA`, `CREATE TEMP` and, where compiled in, `load_extension`. A `set_authorizer` allowlist therefore accepts only `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION` and `SQLITE_RECURSIVE` (for `WITH RECURSIVE`, e.g. a calendar of days) and denies every other action, including the `pragma_*` table functions. `load_extension` is denied by name even though Python's `sqlite3` already disables it. Denied statements are refused at prepare time, before anything runs.
- One statement only: Python's `sqlite3` already refuses a second one, and that refusal is reported as such. Empty or comment-only input is rejected.
- `set_progress_handler` aborts a query after 5 s; the result is capped at 500 rows with a `truncated` flag. The time budget alone cannot bound memory: one call such as `zeroblob(1e9)` or a large `group_concat` builds a huge value in a few VM steps. So `SQLITE_LIMIT_LENGTH` is lowered to 100 KB per string/BLOB for the query (restored afterwards); past it SQLite raises `SQLITE_TOOBIG` (`printf` returns NULL). BLOB values (the content-hash keys) come back as hex and non-finite floats as `"inf"`/`"nan"` strings, so the text output stays valid JSON.
- The tool description carries the schema and its traps: `CAST(value AS REAL)`, filtering on the `(type, local_date)` index, raw units in `workout_stat`, and that `SUM` over `record` double-counts parallel sources (ADR-0010) — preferring the fixed tools when they fit.
**Consequence:** With the flag on, ad-hoc questions are answerable without a code change, at the cost of a less predictable token bill (up to 500 rows of up to 100 KB values) and of numbers the model computes itself, without the dedupe and unit normalization of the fixed tools. Raw rows of the user's own data reach the model's context, which is why it is opt-in. The time budget is wall-clock, so a slow disk can stop a query that would pass on a fast one.

## ADR-0014 — Weekly Markdown report built on the query layer
**Status:** accepted
**Context:** A weekly review in a notes vault (Obsidian + Dataview) needs numbers it can chart across weeks and regenerate for free. The `weekly_review` prompt gives free text that changes between runs and needs an MCP client and an LLM call. A report that computes its own numbers could also drift from what the tools return.
**Decision:**
- `apple-health-export-mcp-report` (`report.py`, no FastMCP import) renders one Monday–Sunday week from `queries.quantity` / `sleep` / `workouts` only. It runs no SQL of its own, so dedupe (ADR-0010) and unit normalization (ADR-0011) apply unchanged.
- Weekly rules on top of those aggregates. Steps and active energy are totals of the per-day deduped sums. `steps_daily_avg` divides by the days that have step data, not by 7, so a week where the export starts or ends midweek is not diluted. Resting HR and HRV are the sample mean, which is the daily averages weighted by their sample counts. Sleep counts nights with time asleep. A night with only `InBed` (legacy devices) counts as no sleep, rather than passing in-bed time off as sleep.
- A missing metric is `null`, never `0`. Counts (`sleep_nights`, `workouts_count`) are `0`. The note has no generation timestamp.
- The note is rewritten only when its content changes: a temp file is renamed over it, the file is owner-only (`0600`) like the DB, and a failed write leaves nothing behind.
**Consequence:** The same DB always gives the same file, and a rerun on unchanged data does not touch it, so vault sync stays quiet. The note is generated output: hand edits are lost on regeneration. Weeks follow `local_date` (ADR-0009), so a week split across time zones follows each sample's own offset.
