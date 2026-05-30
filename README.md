# GarminDrive v1.1

This project syncs Strava activity history and Garmin Connect health data into Google Drive folders that ChatGPT can use as project sources.

Recommended flow:

1. Garmin watch syncs activities and wellness data to Garmin Connect.
2. Garmin Connect auto-uploads activities to Strava.
3. Render polls Strava every 10 minutes.
4. Render syncs Garmin wellness data every 2 hours.
5. The app stores durable tokens and sync state in hidden Google Drive appData because Render disks are ephemeral.
6. Visible Drive files are published under `Projects/Run History` and `Projects/Health Data`.

Strava remains the source of truth for activities and runs. Garmin Connect is used for recovery and wellness data such as Body Battery, all-day heart rate, resting heart rate, stress, HRV, sleep, respiration, SpO2, and training readiness when your account/device exposes them.

## Drive Output

Visible folders, intended for ChatGPT:

- `Projects/Run History`
  - `Run History Index` Google Doc
  - `Run History Data.csv`
  - `Mile Splits Data.csv`
  - `Recent Mile Splits.csv`
  - current and previous yearly run Google Docs, such as `Runs 2026`
  - `Past Summary/` with older yearly Google Docs
  - `Maps/Recent Activity Map.html`
  - `Maps/All Time Activity Map.html`
  - `Raw Data/Run History Data.json`
  - `Raw Data/Runs/YYYY/{date}_{sport-and-name}_{activity_id}.json`
  - `Raw Data/Routes/YYYY/{date}_{sport-and-name}_{activity_id}.geojson`
  - `Raw Data/All Run Routes.geojson`
  - `Raw Data/Heatmaps/All Time Activity Map Data.json`
- `Projects/Health Data`
  - `Health History Data.csv`
  - `Recent Recovery Metrics.csv`
  - `Recovery Summary for ChatGPT` Google Doc
  - `Raw Health/Health History Data.json`
  - `Raw Health/YYYY/YYYY-MM-DD.json`

Local generated output is written under this repo's `Projects/` directory, which is ignored by git, whenever a sync renders outputs. ChatGPT-facing data is exported as Google Docs, Markdown, and CSV; machine-readable JSON is preserved under raw folders so the app does not lose archive detail.

The sync skips visible Drive publishing when the hidden run-history digest is unchanged. After changing the generated output layout, run a one-time `--force-upload` sync to rebuild missing visible files such as `Run History Data.csv` or `Mile Splits Data.csv`.

Hidden Google Drive app data, intended only for this app:

- `strava_token.json`
- `sync_state.json`
- `run_history.json`
- `raw_manifest.json`
- `garmin_token.json`
- `garmin_health_sync_state.json`
- `garmin_health_history.json`
- `garmin_health_raw_manifest.json`

## One-Time Setup

### 1. Create a Strava app

Create an app in Strava API settings. Set the callback domain to:

```text
localhost
```

Copy `.env.example` to `.env`, then set:

```dotenv
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=your-secret
STRAVA_SCOPE=activity:read_all
STRAVA_ACTIVITY_SPORT_TYPES=Run,TrailRun,VirtualRun,Treadmill,TrackRun,Ride,VirtualRide,MountainBikeRide,GravelRide,EBikeRide,EMountainBikeRide
```

Use `activity:read_all` if you want private activities included. `STRAVA_ACTIVITY_SPORT_TYPES` controls which Strava activity types are included in run history.

### 2. Create a Google OAuth desktop client

In Google Cloud:

1. Create or select a project.
2. Enable the Google Drive API.
3. Configure the OAuth consent screen for personal/internal use.
4. Create an OAuth Client ID with application type `Desktop app`.
5. Download the JSON file into this repo as `client_secret_google.json`.

This app requests:

- `drive.file`, to create and update the visible ChatGPT folders/files
- `drive.appdata`, to store hidden rotating tokens and sync state

### 3. Configure Drive folders

The v1.1 defaults create this visible structure:

```dotenv
GOOGLE_DRIVE_PROJECTS_FOLDER_NAME=Projects
GOOGLE_DRIVE_RUN_FOLDER_NAME=Run History
GOOGLE_DRIVE_HEALTH_FOLDER_NAME=Health Data
GOOGLE_DRIVE_PROJECTS_FOLDER_ID=
GOOGLE_DRIVE_RUN_FOLDER_ID=
GOOGLE_DRIVE_HEALTH_FOLDER_ID=
```

Leave IDs blank unless you want to pin the app to existing folders. Legacy `GOOGLE_DRIVE_FOLDER_ID` / `GOOGLE_DRIVE_FOLDER_NAME` still work for older single-folder run-history installs when no new folder settings are provided.

### 4. Install locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 5. Authorize Strava, Google, and Garmin

```powershell
python -m garmin_drive auth-strava
python -m garmin_drive auth-google
python -m garmin_drive auth-garmin
```

`auth-garmin` uses the unofficial `garminconnect` library and saves a local tokenstore at `.data/tokens/garmin_token.json`. If Garmin asks for MFA, enter the code when prompted. Do not commit `.data/`, Garmin credentials, or token files.

### 6. Bootstrap hidden Drive state

```powershell
python -m garmin_drive bootstrap-appdata
python -m garmin_drive bootstrap-garmin-appdata
```

This uploads Strava and Garmin tokens into Google Drive's hidden `appDataFolder`. Render can then refresh/reuse tokens without relying on your PC or persistent local disk.

### 7. Test using the same backend as Render

```powershell
python -m garmin_drive sync-strava --state-backend drive --days 30 --enrich missing --publish-raw
python -m garmin_drive sync-garmin-health --state-backend drive --days 14
```

For a Garmin health backfill:

```powershell
python -m garmin_drive backfill-garmin-health --state-backend drive --start-date 2024-01-01 --end-date 2026-05-29
```

Backfill skips days already listed in the hidden Garmin health raw manifest. Add `--force-refetch` to rebuild existing raw health days.

For a larger Garmin history pull, use the chunked script so each batch updates Drive appData before the next batch starts:

```powershell
.\scripts\backfill_garmin_health.ps1 -StartDate 2024-01-01 -EndDate 2026-05-29 -ChunkDays 30 -PauseSeconds 10
```

## Render Setup

Connect this repo as a Render Blueprint using `render.yaml`.

The blueprint defines:

- `garmin-drive-sync`, every 10 minutes:
  - `python -m garmin_drive sync-strava --days 14 --max-pages 5 --enrich missing --publish-raw --skip-maps --recent-mile-days 14 --request-budget 900`
- `garmin-health-sync`, every 2 hours:
  - `python -m garmin_drive sync-garmin-health --days 14 --state-backend drive`

Set these Render secrets:

| Secret | Required | Value |
| --- | --- | --- |
| `STRAVA_CLIENT_ID` | yes, Strava cron | Strava app client ID |
| `STRAVA_CLIENT_SECRET` | yes, Strava cron | Strava app client secret |
| `GOOGLE_TOKEN_JSON` | yes, both crons | Contents of `.data/tokens/google_token.json` |

Garmin credentials are not required on Render after `bootstrap-garmin-appdata` succeeds. The Garmin cron reads `garmin_token.json` from Drive appData and writes refreshed tokens back there.

## Local Commands

```powershell
python -m garmin_drive auth-strava
python -m garmin_drive auth-google
python -m garmin_drive auth-garmin
python -m garmin_drive bootstrap-appdata
python -m garmin_drive bootstrap-garmin-appdata
python -m garmin_drive sync-strava --state-backend drive --days 30 --enrich missing --publish-raw
python -m garmin_drive sync-garmin-health --state-backend drive --days 14
python -m garmin_drive backfill-garmin-health --state-backend drive --start-date 2024-01-01 --end-date 2026-05-29
python -m garmin_drive sync-all --state-backend drive --days 14 --health-days 14
python -m garmin_drive delete-run 123456789 --state-backend drive
.\scripts\backfill_full_history.ps1 -Days 3650 -RequestBudget 900
.\scripts\backfill_garmin_health.ps1 -StartDate 2024-01-01 -EndDate 2026-05-29 -ChunkDays 30
.\scripts\delete_run.ps1 -ActivityId 123456789
.\scripts\publish_cached_archive.ps1 -TrashOldIdFiles
```

Use `--no-upload` to update local output and hidden state without publishing visible Drive files. Use `--force-upload` to rebuild visible Drive files when generated output files are missing or the visible folder layout has changed. Use `--force-refetch` with Garmin range backfills when you want to overwrite raw health days that were already archived.

Use `--skip-maps` on memory-constrained scheduled Strava syncs. It still publishes run history, CSVs, recent mile splits, and per-activity raw archive files, but skips rebuilding the heavyweight aggregate map files: `Maps/*.html`, `Raw Data/All Run Routes.geojson`, and `Raw Data/Heatmaps/All Time Activity Map Data.json`. Rebuild those occasionally from a local machine with `.\scripts\publish_cached_archive.ps1 -TrashOldIdFiles`.

To repair missing visible run-history outputs without republishing every detailed raw archive file:

```powershell
python -m garmin_drive sync-strava --state-backend drive --days 14 --max-pages 5 --enrich missing --recent-mile-days 14 --request-budget 900 --force-upload --no-publish-raw --skip-maps
```

To remove one bad activity from history, pass its Strava/source activity ID:

```powershell
.\scripts\delete_run.ps1 -ActivityId 123456789
```

`delete-run` removes the matching entry from hidden run history, deletes matching local raw/cache files, moves matching visible Drive raw files to trash when using the Drive backend, prunes raw manifest entries, and regenerates the summary outputs. It also records the activity ID in hidden sync state so future Strava syncs do not re-import it. It is safe to rerun if part of the activity was already removed; missing history or raw pieces are skipped.

## ChatGPT Setup

Connect Google Drive in ChatGPT settings, then connect both project source folders:

- `Projects/Run History`
- `Projects/Health Data`

Example prompt:

```text
Use my Run History and Health Data folders together. Summarize my last 8 weeks of training, compare activity load against sleep duration, resting heart rate, stress, Body Battery minimum/maximum, respiration, and SpO2 trends, and call out recovery risks.
```
