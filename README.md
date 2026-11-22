# apple-health-export-mcp

[![CI](https://github.com/burakdirin/apple-health-export-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/burakdirin/apple-health-export-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/apple-health-export-mcp.svg)](https://pypi.org/project/apple-health-export-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/apple-health-export-mcp.svg)](https://pypi.org/project/apple-health-export-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant query your **Apple Health** data — sleep, heart rate, steps, body mass, workouts, and any other metric in your export.

Apple has no live export API, so this works on a **snapshot**: you export your health archive once, ingest it into a local SQLite database, then the server answers queries against it. See [`docs/adr.md`](docs/adr.md) for why.

> **Privacy:** your health data never leaves your machine and is never committed to git (`.gitignore` excludes `*.zip` / `*.db`).
> The database is created owner-only (`0600`, its new directory `0700`), and the MCP server opens it **read-only**.

## How it works

```
export.zip ──(ingest, one-time ~1-3 min)──► health.db (SQLite) ──◄── MCP server queries
```

## Setup

1. **Export your data** on iPhone: _Settings → Health → tap your photo → Export All Health Data_. AirDrop/save the resulting `export.zip`.

2. **Install** with [uv](https://docs.astral.sh/uv/). Installing once (rather than
   `uvx` on every launch) keeps the process tree shallow, which matters for clean
   shutdown — see [Shutdown & process model](#shutdown--process-model).

   ```bash
   uv tool install git+https://github.com/chienchuanw/apple-health-export-mcp@v0.3.0
   # PyPI (`uv tool install apple-health-export-mcp`) has 0.1.1, without the 0.2.0+
   # changes (workout statistics, read-only server, private DB file, query_sql).
   ```

   This puts three commands on your PATH: `apple-health-export-mcp` (the server),
   `apple-health-export-mcp-ingest` and `apple-health-export-mcp-report` (see
   [Weekly report](#weekly-report)).

3. **Ingest** the archive (one time per new export):

   ```bash
   AH_DB_PATH=~/.local/share/apple-health-export-mcp/health.db \
     apple-health-export-mcp-ingest ~/Downloads/export.zip
   ```

   Re-ingesting rebuilds the database from that export alone and then swaps it in,
   so it is also how an existing database picks up newly supported data (e.g.
   workout distance / energy / heart rate), and how entries you edited or deleted
   in the Health app disappear. Run it again on the same or a newer `export.zip`.
   While it runs, the old database stays in place and readable. The new one is
   built next to it as `health.db.building`, so you need about twice its size in
   free disk space.

   Two safety valves:

   - If the new export has under 90% of the current database's records (e.g. a
     truncated export), or the current database can't be read to compare, ingest
     keeps the current database and exits 1. Re-run with `--force` to replace it
     anyway.
   - `--merge` adds the export to the existing database instead (the previous
     behaviour), for when a newer export intentionally lacks older history, such
     as a new phone without Health iCloud sync. With `--merge`, rows the newer
     export no longer has are kept, and an entry edited on the phone stays in
     both versions.

   On Windows the swap fails while another process (e.g. the MCP server) has the
   database open. Ingest then exits 1 and the old database is kept as it was.

   For a recurring export, point ingest at a drop folder instead of a file (see
   [Drop-folder ingest](#drop-folder-ingest)).

4. **Add to your MCP client** (e.g. Claude Code `.mcp.json`). The bare command
   starts the stdio server; point it at the DB you just built.

   **Installed (recommended)** — invoke the binary directly (no wrapper process):

   ```json
   {
     "mcpServers": {
       "apple-health": {
         "command": "apple-health-export-mcp",
         "env": { "AH_DB_PATH": "/Users/you/.local/share/apple-health-export-mcp/health.db" }
       }
     }
   }
   ```

   Use an absolute path to the binary (`which apple-health-export-mcp`) if your
   client doesn't inherit your `PATH`.

   **Or via `uvx`** (no install; the bare package name runs the server):

   ```json
   {
     "mcpServers": {
       "apple-health": {
         "command": "uvx",
         "args": ["apple-health-export-mcp"],
         "env": { "AH_DB_PATH": "/Users/you/.local/share/apple-health-export-mcp/health.db" }
       }
     }
   }
   ```

   `claude mcp add` equivalent:

   ```bash
   claude mcp add apple-health --env AH_DB_PATH=~/.local/share/apple-health-export-mcp/health.db \
     -- apple-health-export-mcp
   ```

## Shutdown & process model

The server is a single process that shuts down on **stdin EOF** — the MCP
spec's primary shutdown signal — so closing your client terminates it cleanly.
Avoid extra wrapper layers (`uv run … fastmcp run …`): `uv`/`uvx` stay in the
process tree as a parent and only *conditionally* forward signals, so a wrapped
server can be orphaned when the client exits or you press Ctrl+C. Installing the
tool and launching the binary directly (config above) gives the shallowest tree
and the most reliable cleanup. `fastmcp run fastmcp.json` is for local dev only.

## Tools

| Tool                                                  | Returns                                                                     |
| ----------------------------------------------------- | --------------------------------------------------------------------------- |
| `list_types()`                                        | Which metrics exist in _your_ data + row counts and date spans (discovery)  |
| `list_sources(type)`                                  | Which devices/apps wrote a metric (counts, date spans)                      |
| `get_quantity(type, start, end, agg, bucket, source)` | Per day/week/month/all aggregate for a numeric metric (steps, HR, weight…)  |
| `get_sleep(start, end)`                               | Per-night sleep stage durations                                             |
| `get_workouts(start, end)`                            | Per activity: count, minutes (total/avg/max), km, active kcal, avg HR       |
| `get_baseline(type, end, recent_days, baseline_days, agg)` | Recent mean vs your own preceding baseline: delta, delta %, z-score     |
| `get_training_load(end, weeks)`                       | Per week: sessions, minutes, km, kcal, and acute:chronic ratio (acwr)       |

All query tools require a date range and return **aggregates only** — never raw rows (ADR-0008).
`get_quantity` with `agg="sum"` auto-deduplicates parallel devices (Watch + iPhone + apps) so totals
aren't inflated (ADR-0010); pass `source` (see `list_sources`) to force one device.
`get_workouts` normalizes units (mi → km, kJ → kcal, hours/seconds → minutes) and weights the
average heart rate by workout duration; a field is `null` when the export has no such data (ADR-0011).
A workout without a heart-rate statistic uses the heart-rate samples recorded during it, if there is
at least one per minute of its start-to-end window (ADR-0012).
`max_min` is the longest single workout: one far above `avg_min` usually means a session was left
running, so it is surfaced rather than silently excluded.
`get_baseline` compares the last `recent_days` (default 7) ending at `end` with the `baseline_days`
(default 28) just before them, using the same per-day values as `get_quantity`. Pass
`HKCategoryTypeIdentifierSleepAnalysis` to compare minutes asleep per night. `z` is the change in
baseline standard deviations, and it is `null` with fewer than 7 baseline days. `get_training_load`
gives weekly workout minutes and `acwr`, a week's minutes divided by the mean of the 4 weeks before
it. Load is minutes-based, because the export has no per-workout intensity (ADR-0017). The
`readiness_check` and `weekly_review` prompts use both tools.

### Optional: read-only SQL (`query_sql`)

For questions the tools above can't answer (e.g. "steps on days I ran vs days I didn't"),
set `AH_ENABLE_SQL=1` in the server's `env` to also register `query_sql(sql)`. It runs a single
`SELECT` against the database and is off by default because it returns rows, not just aggregates
(ADR-0013):

- read-only twice over: the connection is `mode=ro`, and an SQLite authorizer refuses anything
  but `SELECT` (no writes, `PRAGMA`, `ATTACH` or `load_extension`);
- one statement per call, stopped after 5 s, at most 500 rows (the result says when it was truncated)
  and 100 KB per value;
- numbers are raw: `SUM` over `record` double-counts parallel devices unless you filter by `source`,
  and workout units are not normalized — the fixed tools stay the better choice when they fit.

```json
"env": { "AH_DB_PATH": "/Users/you/.local/share/apple-health-export-mcp/health.db", "AH_ENABLE_SQL": "1" }
```

## Prompts

Reusable coaching workflows the client can invoke (they orchestrate the tools and reply in your language):

| Prompt                       | Purpose                                            |
| ---------------------------- | -------------------------------------------------- |
| `daily_summary(day?)`        | One day's snapshot                                 |
| `weekly_review(week_of?)`    | Calendar week (Mon–Sun): load vs recovery + advice |
| `monthly_summary(month?)`    | A month in review (YYYY-MM)                        |
| `yearly_summary(year?)`      | A year's fitness trajectory (YYYY)                 |
| `readiness_check()`          | Train hard today? From sleep + recovery markers    |
| `sleep_report(start?, end?)` | Sleep duration, stages, consistency                |

Arguments are optional — they default to today / this week / this month / this year.

## Drop-folder ingest

For a weekly export, save each `export.zip` into one folder (e.g. an iCloud Drive folder
the iPhone's share sheet can reach) and run:

```bash
apple-health-export-mcp-ingest --inbox ~/Library/Mobile\ Documents/com~apple~CloudDocs/HealthInbox --keep 1
```

It is safe to run as often as you like, e.g. from a file watcher. Each run:

- picks the newest `*.zip` in the folder (hidden files, i.e. partial copies, are ignored);
- **does nothing (exit 0)** when that file is not ready: still only an iCloud placeholder
  (`.export.zip.icloud`, macOS 13 and earlier), still being copied (its size changed within
  `--settle` seconds, default 2), or not a complete zip yet. On macOS 14 and later iCloud
  keeps an evicted file under its own name, and reading it downloads it first, so the run
  waits for the download. Set the folder to *Keep Downloaded* in Finder to avoid the wait;
- **does nothing (exit 0)** when that exact file was already ingested: the last successful
  ingest records the zip's SHA-256 (a manual `apple-health-export-mcp-ingest <zip>` counts too);
- otherwise ingests it, and with `--keep N` then deletes all but the newest N zips.
  Without `--keep` nothing is deleted;
- exits 0 with "already running" if another ingest holds the lock (`<db>.lock`).

Errors (no health data in the zip, a corrupt XML, a missing folder) exit 1 with a one-line
`error:` message. A failed ingest records no digest, so the next run retries it.
File locking uses `fcntl` (macOS/Linux).

## Weekly report

`apple-health-export-mcp-report` writes one Monday–Sunday week as a Markdown note,
for a notes vault such as Obsidian. No MCP client or LLM is involved: the numbers come
from the same aggregates the tools return, and the same data always gives the same file.

```bash
apple-health-export-mcp-report --out ~/Vault/Health                 # last completed week
apple-health-export-mcp-report --week-of 2026-11-18 --out ~/Vault/Health
apple-health-export-mcp-report --week 2026-W47 --out ~/Vault/Health  # ISO week
```

It writes `<out>/2026-W47.md` (the folder is created if missing; `AH_REPORT_DIR` can
replace `--out`, and `--db` / `AH_DB_PATH` pick the database as for ingest). The YAML
frontmatter holds the week's numbers, for [Dataview](https://blacksmithgu.github.io/obsidian-dataview/)
queries across weeks:

```yaml
week: 2026-W47
start: 2026-11-16
end: 2026-11-22
steps_total: 61234          # per-day sums, deduplicated across devices
steps_daily_avg: 8748       # over the days that have step data
active_kcal_total: 3410
resting_hr_avg: 54
hrv_avg: 48.5
sleep_asleep_avg_min: 412   # over nights with sleep data
sleep_nights: 7
workouts_count: 4
workout_min_total: 185
workout_km_total: 24.3
workout_kcal_total: 1520
```

The body has a per-day table and a per-activity workout table. A metric with no data
this week is `null` rather than `0`. Counts (`sleep_nights`, `workouts_count`) are `0`.
A night with only in-bed time (older devices) counts as no sleep. The note is regenerated
output, so rerunning overwrites it. Keep your own notes in a separate file. A rerun on
unchanged data leaves the file untouched, so vault sync has nothing to upload. The note
is created owner-only (`0600`), like the database. See ADR-0014.

> **Privacy:** the note contains your health aggregates. If the vault syncs (Obsidian
> Sync, iCloud, git), so does the report. Keep `export.zip` and `health.db` outside it.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
```

`tests/fixtures/synthetic.py` generates a synthetic export with known ground truth
(used by the tests). For a manual ingest benchmark, write a large one and time it:

```bash
uv run python tests/fixtures/synthetic.py /tmp/big.zip 150000   # ~0.8 GB of XML
/usr/bin/time -l apple-health-export-mcp-ingest /tmp/big.zip --db /tmp/big.db
```

## License

MIT
