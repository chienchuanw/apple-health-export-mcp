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

   Right-click the folder and choose **Keep Downloaded** (macOS 14 and later). Without it,
   iCloud may keep a new export only in the cloud, and the ingest then waits for the
   download (see [Gotchas](#gotchas)).

3. **Pick the vault folder** for the notes, e.g. `/Users/you/Documents/Vault/Health`.
   An Obsidian vault kept in iCloud lives under
   `/Users/you/Library/Mobile Documents/iCloud~md~obsidian/Documents/<Vault>/`.

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

# Run either one now, without waiting for its trigger.
launchctl kickstart -k gui/$(id -u)/local.apple-health.ingest
launchctl kickstart -k gui/$(id -u)/local.apple-health.report

# See what happened: the log, and the last exit code.
tail -n 20 ~/Library/Logs/apple-health-ingest.log
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
2. Within a few seconds of the copy finishing, the ingest log should end with `Done: …`.
3. Run `launchctl kickstart -k gui/$(id -u)/local.apple-health.report`. The report log
   then shows `Wrote …/2026-Www.md`, and the note appears in your vault.

To try it without real data, generate a synthetic export from a checkout of this
repository with `uv run python tests/fixtures/synthetic.py /tmp/export.zip 14`, and point
`AH_DB_PATH` and `--out` at scratch locations first.

## Optional: coaching commentary from Claude

The report is plain numbers. To add written commentary, a third step can ask Claude
Code to run the `weekly_review` coaching prompt over the same database and append the
answer to the note. This needs Claude Code installed with the MCP server configured
(see the [README](../README.md#setup)), and every run uses tokens. It is not part of the
tested setup above. Treat it as a starting point:

```bash
claude -p "Use the apple-health MCP tools to review last week like the weekly_review \
prompt does, then append the review under a '## Review' heading to \
/Users/you/Documents/Vault/Health/<week>.md" \
  --allowedTools "mcp__apple-health" "Edit"
```

Keep the numbers in the generated note, and put commentary or your own notes under a
separate heading or in a separate note: rerunning the report overwrites the file.

## Gotchas

- **An export still in iCloud.** On macOS 13 and earlier, an export that is still only in
  the cloud shows up as a hidden `.export.zip.icloud` placeholder, and ingest waits.
  From macOS 14 the file keeps its own name, and the first read downloads it, so the
  ingest simply takes longer. *Keep Downloaded* on the folder avoids both. Opening the
  file in Finder also downloads it.
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
