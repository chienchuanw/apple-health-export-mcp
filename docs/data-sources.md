# Hands-free data sources: evaluation

**Question (#17):** can a scheduled, phone-side export replace the weekly manual
*Export All Health Data* step, and should this tool get a second ingest adapter for it?

**Short answer: not yet.** Neither candidate is a drop-in replacement for `export.xml`.
iOS does not let any app or shortcut read Health data while the iPhone is locked, so
neither can promise a weekly run with no manual step. The decision is in
[ADR-0018](adr.md) (status *proposed*): keep `export.xml` as the source, and revisit
Health Auto Export once the open questions below have been tested on a device.

## How this was evaluated

From documentation and community reports gathered in November 2026 (sources linked
inline). **Nothing here was tested on a device**, and no real health data was used.
Output samples are copied from the vendors' format documentation, with placeholder
values. Statements that could not be verified are marked *unverified*. The questions
are the seven from #17, plus what this tool depends on: per-sample source names for the
per-day dedupe (ADR-0010), local timestamps with an offset (ADR-0009), HealthKit type
identifiers (ADR-0004), and workout statistics (ADR-0011/0012).

## The constraint both share: Health data is locked with the phone

An Apple engineer, on the developer forums in May 2026: "your app is not allowed to read
health data while a device is locked"
([thread 824819](https://developer.apple.com/forums/thread/824819)). Health Auto Export's
own docs say: "Allowing a shortcut to run while locked does not allow it to read locked
health data"
([HAE: schedule with Shortcuts](https://help.healthyapps.dev/en/health-auto-export/automations/schedule-automations-using-shortcuts)).
Shortcuts reports the same failure as "Protected health data is inaccessible"
([Automators forum](https://talk.automators.fm/t/get-workouts-from-health-not-working-when-iphone-locked/13100)).

A weekly run can start on its own, but it only gets data if the phone happens to be
unlocked at that moment. That favours a trigger tied to a time you are using the phone,
and it means tolerating missed runs. A manual export, by contrast, always works, because
you are holding the unlocked phone.

## A) iOS Shortcuts: "Find Health Samples"

1. **Scheduling.** Since iOS 17, a *Time of Day* personal automation can run without
   asking ("Run Immediately"), and it shows a notification
   ([Cassinelli](https://matthewcassinelli.com/automations-run-immediately-shortcuts-notifications/)).
   With the phone locked, Health reads fail (see above). A charger-connected trigger at a
   time you usually unlock might work, but that is unverified.
2. **Coverage.** Quantity and category samples, including steps, heart rate, HRV, resting
   HR, body mass and sleep. Each sample has type, value, unit, start/end date, duration
   and source ([Get Details of Health Sample](https://matthewcassinelli.com/actions/get-details-of-health-sample/)).
   **Workouts are not available.** There is no native workout action; third-party add-ons
   (e.g. Toolbox Pro) are needed and still cannot attach heart rate to a workout
   ([MPU forum](https://talk.macpowerusers.com/t/struggling-to-extract-workout-data-from-watch-via-shortcuts/39152)).
   How sleep stages are labelled is unverified.
3. **Granularity.** Raw samples by default, with optional grouping
   ([Find Health Samples](https://matthewcassinelli.com/actions/find-health-samples/)). A
   week of heart-rate samples may be slow to loop over, which is unverified.
4. **Timestamps.** Date objects: the shortcut has to format them (e.g. ISO 8601). Whether
   the offset is kept depends on that formatting, which is unverified.
5. **Identifiers.** Localized display names ("Steps"), not HealthKit identifiers, so the
   output depends on the phone's language.
6. **Incremental.** A rolling date filter ("in the last 7 days"). There is no "since the
   last run".
7. **Cost, privacy, maintenance.** Free, and the file goes straight to iCloud Drive. But
   the output format is whatever the shortcut builds, so you maintain both the shortcut
   and its parser, across iOS versions.

## B) Health Auto Export (third-party app)

1. **Scheduling.** The app's own background schedule is up to iOS ("iOS decides when
   background execution happens",
   [HAE: automations](https://help.healthyapps.dev/en/health-auto-export/automations)).
   For a fixed time, its docs recommend a Shortcuts automation that runs the app's
   automation. The lock rule applies: "Automations will only run during periods when your
   device is unlocked". It also needs Background App Refresh on and Low Power Mode off.
2. **Coverage.** Steps, heart rate, resting HR, HRV (SDNN), body mass and sleep analysis
   ([supported data](https://help.healthyapps.dev/en/health-auto-export/getting-started/supported-data)).
   Sleep stages are labelled Core / Deep / REM / Awake / In Bed. Workouts (format v2)
   include duration, distance, active energy, average and maximum heart rate, per-workout
   heart-rate data, and the source
   ([workouts format](https://help.healthyapps.dev/en/health-auto-export/export-format/workouts)).
   Per-sample `source` is present "when available"; device names need a setting
   ([source preferences](https://help.healthyapps.dev/en/health-auto-export/getting-started/source-preferences)).
3. **Granularity.** Summarized by default (seconds to years). With *Summarize Data* off,
   it exports individual readings "where available". That is documented for sleep
   segments and HRV, but **not confirmed for steps or heart rate**. CSV output is always
   summarized ([iCloud Drive automations](https://help.healthyapps.dev/en/health-auto-export/automations/icloud-drive)).
   The docs do not say how summarized totals treat parallel sources (Watch + iPhone).
4. **Timestamps.** `yyyy-MM-dd HH:mm:ss Z`, e.g. `2024-02-06 14:30:00 -0800`: local time
   with offset, the same shape as `export.xml`
   ([export format](https://help.healthyapps.dev/en/health-auto-export/export-format)).
5. **Identifiers.** snake_case names (`step_count`), each with a `units` field, not
   HealthKit identifiers
   ([health metrics](https://help.healthyapps.dev/en/health-auto-export/export-format/health-metrics)).
6. **Incremental.** iCloud Drive automations export a fixed period (day, week, month,
   year). "Since last sync" exists only for the REST, MQTT and email targets. Whether a
   run overwrites or appends its file is not documented.
7. **Cost, privacy, maintenance.** Automations need the paid Premium tier (per its pricing
   page at the time of writing,
   [pricing](https://help.healthyapps.dev/en/health-auto-export/purchases/pricing)). With
   iCloud Drive as the target, data goes only to your own iCloud. The App Store privacy
   label (self-reported) says no data is collected. The format is documented, versioned
   (v1/v2), and the app is actively maintained.

Shape of its JSON (from the format docs; values are placeholders):

```json
{"data": {
  "metrics": [{"name": "step_count", "units": "count",
               "data": [{"qty": 0, "date": "2024-02-06 14:30:00 -0800", "source": "…"}]}],
  "workouts": [{"id": "…", "name": "Running",
                "start": "2024-02-06 07:00:00 -0800", "end": "2024-02-06 07:30:00 -0800",
                "duration": 1800, "distance": {"qty": 0, "units": "mi"},
                "activeEnergyBurned": {"qty": 0, "units": "kcal"}}]}}
```

## Comparison

| What this tool needs                        | export.xml (today) | Shortcuts           | Health Auto Export                 |
| ------------------------------------------- | ------------------ | ------------------- | ---------------------------------- |
| Runs without you                            | no (manual)        | only if unlocked    | only if unlocked                   |
| Workouts with statistics (ADR-0011)         | yes                | **no**              | yes                                |
| Sleep stages                                | yes                | unverified labels   | yes                                |
| Raw samples, per source (dedupe, ADR-0010)  | yes                | yes                 | sleep/HRV yes; steps/HR unverified |
| Local time + offset (ADR-0009)              | yes                | if formatted so     | yes                                |
| HealthKit identifiers (ADR-0004)            | yes                | no (localized)      | no (snake_case, mappable)          |
| Full history vs window                      | full snapshot      | rolling window      | fixed window                       |
| Cost                                        | free               | free                | paid tier for automations          |
| Format you depend on                        | Apple's            | your own shortcut   | documented, versioned              |

## Recommendation

- **Shortcuts: no.** Without workouts it cannot cover this tool's training views, and you
  would own a fragile custom format.
- **Health Auto Export: not now; maybe later as a top-up importer, not a replacement.**
  It would need four things. First, a name-to-identifier map (`step_count` →
  `HKQuantityTypeIdentifierStepCount`). Second, a merge into the existing DB, because
  its weekly windows overlap and it is not a full snapshot, so the rebuild of ADR-0016
  cannot apply. Third, an occasional full `export.xml` to fill gaps from missed runs.
  Fourth, and most important, raw per-source steps: if steps only arrive summarized,
  the per-day dominant-source dedupe cannot run, and totals would follow whatever the
  app's summarizing does.
- **Meanwhile**, the manual export plus `--inbox` and the launchd agents
  ([automation guide](automation.md)) costs about a minute a week and keeps every
  guarantee the tool makes.

## What a device test would need to answer

1. With *Summarize Data* off, are steps and heart rate raw samples per source, or already
   merged statistics?
2. Does an iCloud Drive automation set to *Week* overwrite one file or write a new one per
   run, and with what file name?
3. How often does a weekly run that fires while the phone is unlocked (e.g. on charger
   connect at a usual time) actually succeed?
4. Are the `source` values the same device names as `export.xml`'s `sourceName`?
5. Do workout totals match `export.xml`'s `WorkoutStatistics` for the same workout?

If those answers come back favourable, the follow-up is a separate adapter (e.g.
`ingest_hae.py`) that maps into the existing `record` / `workout` / `workout_stat`
schema. It would come with its own synthetic fixture and ground truth, like
`tests/fixtures/synthetic.py`.
