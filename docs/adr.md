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
- `get_workouts` adds `total_distance_km` (all `HKQuantityTypeIdentifierDistance*` types, so running, cycling and swimming all count), `total_energy_kcal` (active energy) and `avg_heart_rate` (each workout's average weighted by its duration, over workouts that have one). Still aggregates only (ADR-0008).
**Consequence:** Distance/energy/HR questions are answerable per activity and range, across export eras. Known ceilings: merged segments assume one unit per type within a workout; a workout without heart-rate data does not dilute `avg_heart_rate` but also contributes nothing to it. <!-- ponytail: per-segment rows only if a real need appears -->
