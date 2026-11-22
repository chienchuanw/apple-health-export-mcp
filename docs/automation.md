# Weekly automation: iPhone export → Mac → notes vault

This guide sets up a weekly pipeline on macOS:

```
iPhone (once a week, ~1 min)                Mac (automatic)
Export All Health Data ──► iCloud Drive ──► launchd watches the folder ──► ingest
                           HealthInbox/                                     │
                                            launchd, Monday 08:00 ──► weekly report
                                                                            │
                                                          your vault/Health/2026-W47.md
```

What is automatic and what is not:

- **The iPhone export stays manual.** Apple's *Export All Health Data* has no Shortcuts
  action and no API, so nothing can trigger it for you. It takes about a minute a week.
  A weekly reminder (below) keeps it a habit. For an export with no manual step, see
  [#17](https://github.com/chienchuanw/apple-health-export-mcp/issues/17), which
  evaluates other data sources.
- **Everything on the Mac is automatic.** When a new export lands in the inbox folder,
  it is ingested. Every Monday the previous week's report is written into your vault.

## Where files live, and why

| What                    | Where                                                         |
| ----------------------- | ------------------------------------------------------------- |
| Export inbox            | iCloud Drive: `HealthInbox/`                                  |
| Database                | default `~/.local/share/apple-health-export-mcp/health.db`    |
| Weekly notes            | your vault, e.g. `<vault>/Health/`                            |

Keep `export.zip` and `health.db` **outside** the vault:

- Each export is your **full** history and grows every week, often to hundreds of MB.
  A synced vault (Obsidian Sync, iCloud, git) would upload a new copy each week.
- Whatever is in the vault is synced, and so is anything that syncs it. The database
  is created owner-only (`0600`), but that only protects it on this Mac's disk.

The weekly notes hold aggregates only (totals, averages, per-day numbers), so they are
what belongs in the vault. They are also created owner-only.

## One-time setup

1. **Install** the tool (see the [README](../README.md#setup)) and note the absolute paths
   of its commands. launchd does not use your shell's `PATH`:

   ```bash
   which apple-health-export-mcp-ingest apple-health-export-mcp-report
   # e.g. /Users/you/.local/bin/apple-health-export-mcp-ingest
   ```

2. **Create the inbox folder** in iCloud Drive. In Finder: *iCloud Drive → New Folder →
   `HealthInbox`*. Its path on the Mac is:

   ```
   /Users/you/Library/Mobile Documents/com~apple~CloudDocs/HealthInbox
   ```

   Make sure new exports are stored on the Mac, not only in the cloud. On macOS 15
   (Sequoia) and later, right-click the folder and choose **Keep Downloaded**. On older
   macOS, turn off *Optimize Mac Storage* in the iCloud Drive settings (under your name
   in System Settings). Otherwise the ingest waits for the download (see
   [Gotchas](#gotchas)).

3. **Pick the vault folder** for the notes, e.g. `/Users/you/Documents/Vault/Health`.
   An Obsidian vault kept in iCloud lives under
   `/Users/you/Library/Mobile Documents/iCloud~md~obsidian/Documents/<Vault>/`.
   `Documents` and iCloud Drive are privacy-protected folders. A background job writing
   there may need the permission described under [Gotchas](#gotchas).

## Every week on the iPhone

In the **Health** app, tap your profile picture (top right), then *Export All Health Data →
Export*. It takes a moment to prepare. When the share sheet appears: *Save to Files →
iCloud Drive → HealthInbox → Save*.

To be reminded, add a weekly automation in the Shortcuts app: *Automation → New
Automation → Time of Day*, e.g. Sunday 21:00, then *Weekly*, then *Show Notification*
("Export Health data to HealthInbox").

## The two launchd agents

In both files, replace every `/Users/you` path with yours. Use the command paths from
`which`, your inbox folder, and your vault folder. The `local.apple-health.*` labels can
stay as they are.

### 1. Ingest when a new export arrives

`~/Library/LaunchAgents/local.apple-health.ingest.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.apple-health.ingest</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.local/bin/apple-health-export-mcp-ingest</string>
    <string>--inbox</string>
    <string>/Users/you/Library/Mobile Documents/com~apple~CloudDocs/HealthInbox</string>
    <string>--keep</string>
    <string>1</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AH_DB_PATH</key>
    <string>/Users/you/.local/share/apple-health-export-mcp/health.db</string>
  </dict>
  <key>WatchPaths</key>
  <array>
    <string>/Users/you/Library/Mobile Documents/com~apple~CloudDocs/HealthInbox</string>
  </array>
  <key>StandardOutPath</key>
  <string>/Users/you/Library/Logs/apple-health-ingest.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/Library/Logs/apple-health-ingest.log</string>
</dict>
</plist>
```

`WatchPaths` runs the job whenever the folder changes. That happens several times per
copy, again when `--keep 1` deletes the previous export, and sometimes once when the
agent is loaded. That is fine: `--inbox` exits 0 without doing anything when the newest export
is still downloading or copying, or was already ingested. See
[Drop-folder ingest](../README.md#drop-folder-ingest).

### 2. Write last week's report every Monday

`~/Library/LaunchAgents/local.apple-health.report.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.apple-health.report</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/.local/bin/apple-health-export-mcp-report</string>
    <string>--out</string>
    <string>/Users/you/Documents/Vault/Health</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AH_DB_PATH</key>
    <string>/Users/you/.local/share/apple-health-export-mcp/health.db</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>1</integer>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/you/Library/Logs/apple-health-report.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/Library/Logs/apple-health-report.log</string>
</dict>
</plist>
```

Without `--week` / `--week-of`, the report covers the last completed Monday–Sunday week.
`Weekday` 1 is Monday. If the Mac is asleep at 08:00, launchd runs the job when it wakes.
If it is shut down at that time, that week's run is skipped. Run the report by hand, or
schedule it for a time the Mac is usually on.

The report only knows what was ingested. If you export on Monday after 08:00, the
note for the previous week misses the latest days. Run the report command by hand once
the export is in (it overwrites the note), or schedule the agent later in the day.

## Install, test, inspect, uninstall

```bash
# Check the files, then load both agents (they stay loaded across reboots).
plutil -lint ~/Library/LaunchAgents/local.apple-health.*.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.apple-health.ingest.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.apple-health.report.plist

# Run either one now, without waiting for its trigger. (Plain kickstart: with -k it
# would first kill a run in progress, e.g. an ingest of a large export.)
launchctl kickstart gui/$(id -u)/local.apple-health.ingest
launchctl kickstart gui/$(id -u)/local.apple-health.report

# See what happened: the logs, and the last exit code.
tail -n 20 ~/Library/Logs/apple-health-ingest.log
tail -n 20 ~/Library/Logs/apple-health-report.log
launchctl print gui/$(id -u)/local.apple-health.ingest | grep -E "last exit code|runs"

# After editing a plist: unload, then load again.
launchctl bootout gui/$(id -u)/local.apple-health.ingest
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.apple-health.ingest.plist

# Uninstall.
launchctl bootout gui/$(id -u)/local.apple-health.ingest
launchctl bootout gui/$(id -u)/local.apple-health.report
rm ~/Library/LaunchAgents/local.apple-health.{ingest,report}.plist
```

What the ingest log lines mean:

| Line                                               | Meaning                                          |
| -------------------------------------------------- | ------------------------------------------------ |
| `Not ready: … not downloaded yet / still being written` | Waiting for iCloud or the copy. Nothing to do. |
| `Nothing new: export.zip is already ingested.`     | That exact file is already in the database.       |
| `Done: N records, …`                               | Ingested. The database was rebuilt and swapped in. |
| `Another ingest is already running; skipping.`     | An earlier trigger is still working.              |
| `error: the new export has N records, …`           | Shrink guard (exit 1): see below.                 |

## Check that it works

1. Load both agents, then save an export into `HealthInbox` (or copy one there in Finder).
2. Once the copy has finished and the ingest ran (a real export takes a few minutes), the
   ingest log contains `Done: …`. Lines after it are normal: `--keep 1` deleting the
   previous export triggers another run, which logs `Nothing new: … is already ingested.`
3. Run `launchctl kickstart gui/$(id -u)/local.apple-health.report`. The report log then
   shows `Wrote …/2026-Www.md`, and the note appears in your vault.

To try the pipeline without real data, use **scratch copies of both plists**, with a
different `Label` (e.g. `local.apple-health-test.ingest`), and a scratch folder for the
inbox (`--inbox` and `WatchPaths`), `AH_DB_PATH` and `--out`. Do not point a test at the
real `HealthInbox`: with `--keep 1`, a synthetic zip there becomes the newest export, and
the real one is deleted. From a checkout of this repository,
`uv run python tests/fixtures/synthetic.py /tmp/export.zip 14` writes a 14-day export
starting 2026-10-01. Its data falls in October 2026, so test the report with
`--week-of 2026-10-05`, not the default last week (which would be all `null`).

## Optional: coaching commentary from Claude

The report is plain numbers. To add written commentary, a third step can ask Claude
Code for a review of the same week and append the answer to the note. This needs Claude
Code installed, and every run uses tokens. It is not part of the tested setup above, so
treat it as a starting point.

Claude gets no built-in tools (`--tools ""`) and only this MCP server
(`--strict-mcp-config`), whose tools are read-only. Its answer goes to standard output,
and the shell appends it to the note, so no file-edit permission is needed. Put the
server in its own config file (`/Users/you/.config/apple-health.mcp.json`):

```json
{
  "mcpServers": {
    "apple-health": {
      "command": "/Users/you/.local/bin/apple-health-export-mcp",
      "env": { "AH_DB_PATH": "/Users/you/.local/share/apple-health-export-mcp/health.db" }
    }
  }
}
```

Then save this script, e.g. as `/Users/you/bin/health-review.sh` (`chmod +x` it). Use the
absolute path from `which claude`, because launchd has no `PATH`:

```bash
#!/bin/sh
monday=$(date -v-7d -v-mon +%F); sunday=$(date -v-7d -v-mon -v+6d +%F)
week=$(date -v-7d +%G-W%V)  # the note the report just wrote, e.g. 2026-W47
if review=$(/Users/you/.local/bin/claude -p "Review my health and training for ${monday} \
to ${sunday}. Use get_training_load (end=${sunday}, weeks=5), get_workouts, get_sleep, and \
get_baseline (end=${sunday}) for HKQuantityTypeIdentifierRestingHeartRate and \
HKQuantityTypeIdentifierHeartRateVariabilitySDNN. Be concise; end with one recommendation." \
    --mcp-config /Users/you/.config/apple-health.mcp.json --strict-mcp-config \
    --tools "" --allowedTools "mcp__apple-health"); then
  printf '\n## Review\n\n%s\n' "$review" >> "/Users/you/Documents/Vault/Health/${week}.md"
else
  printf '%s\n' "$review" >&2; exit 1
fi
```

Run it only after the report succeeded, by chaining the two with `&&` in the report
agent's `ProgramArguments`: `/bin/sh`, `-c`, and
`/Users/you/.local/bin/apple-health-export-mcp-report && /Users/you/bin/health-review.sh`.
With `;` instead, a failed report would leave a note holding only the review heading.

The review is appended only if `claude` succeeds, so a failure (e.g. not logged in) never
ends up in the note: its message goes to the error log and the job exits 1. The report compares the whole file with what it would write, so
**any** later rerun of the report for that week overwrites the note and drops the review.
Keep anything you want to keep in a separate note.

## Gotchas

- **An export still in iCloud.** On macOS 13 and earlier, an export that is still only in
  the cloud shows up as a hidden `.export.zip.icloud` placeholder, and ingest waits
  (`Not ready: … not downloaded yet`). From macOS 14 the file keeps its own name, and the
  first read downloads it, so the ingest simply takes longer. To keep exports on the
  Mac, use *Keep Downloaded* on the folder (macOS 15 and later) or turn off *Optimize Mac
  Storage* (older macOS). Opening the file in Finder downloads it once.
- **"Operation not permitted" in the log.** macOS privacy controls (TCC) can block a
  background job from reading iCloud Drive or Documents. Grant **Full Disk Access**
  (*System Settings → Privacy & Security*) to the Python interpreter the tool runs with.
  `head -1 "$(which apple-health-export-mcp-ingest)"` prints its path after `#!`, and
  `realpath` on that path gives the real file to add, since the first one is often a
  symlink.
  Alternatively, keep the database and vault folders outside protected locations.
- **The shrink guard.** If a new export has under 90% of the current database's records
  (e.g. a truncated export), ingest keeps the current database and logs an `error:` line.
  The rebuild is retried on every trigger until you act. Run the ingest by hand with
  `--force` to accept the smaller export, or with `--merge` to add it to the existing
  history (see the [README](../README.md#setup)).
- **Disk space.** Each ingest builds the new database next to the old one, so it needs
  about twice its size in free space.
- **A missed Monday.** A Mac that is asleep at 08:00 runs the report when it wakes. One
  that is shut down skips that run. The next Monday's run covers only its own previous
  week, so write a skipped week by hand with `--week 2026-Www`.
- **One export at a time.** `--keep 1` deletes older exports only after a successful ingest
  or a confirmed "already ingested". Without `--keep`, the folder keeps every export.
