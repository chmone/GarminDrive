# Garmin/Strava Run History to Google Drive

This project syncs running history into a Google Drive folder that ChatGPT can use as a source.

Recommended flow:

1. Garmin watch syncs to Garmin Connect.
2. Garmin Connect syncs activities to Strava.
3. GitHub Actions runs this repo every 4 hours.
4. The job reads Strava, updates hidden sync state in Google Drive, and publishes readable run history files into a visible Google Drive folder.
5. ChatGPT's Google Drive connector syncs that folder.

Strava is the first source because it has a normal OAuth API. Direct Garmin ingestion can be added later, but Garmin's official Health API requires Garmin Developer Program approval and may require a license fee.

## What Gets Written To Drive

Visible folder, intended for ChatGPT:

- `Run History Index` Google Doc
- `Run History Data.json`
- `Run History Data.csv`
- `Recent Mile Splits.json`
- `Recent Mile Splits.csv`
- `Recent Run Map.html`
- yearly Google Docs such as `Runs 2026`
- `Raw Data/`
  - `Runs/YYYY/{activity_id}.json`, with detailed activity data, all available streams, derived mile splits, and fetch metadata
  - `Routes/YYYY/{activity_id}.geojson`, with exact route geometry when Strava provides GPS data
  - `All Run Routes.geojson`
  - `All Runs Map.html`

Hidden Google Drive app data, intended only for this app:

- `strava_token.json`
- `sync_state.json`
- `run_history.json`
- `raw_manifest.json`

The hidden app data is important because Strava refresh tokens can rotate. GitHub Actions runners start fresh every run, so the latest Strava token must live somewhere durable.

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

Use `activity:read` if you only want public/followers-visible activities. Use `activity:read_all` if you want private activities included.

`STRAVA_ACTIVITY_SPORT_TYPES` controls which Strava activities are included in the history. The default includes normal runs, trail/treadmill/virtual runs, and common bike ride types. Indoor bike rides may arrive as `VirtualRide` or as `Ride` with Strava's trainer flag, so both are included.

### 2. Create a Google OAuth desktop client

In Google Cloud:

1. Create or select a project.
2. Enable the Google Drive API.
3. Configure the OAuth consent screen for personal/internal use.
4. Create an OAuth Client ID with application type `Desktop app`.
5. Download the JSON file into this repo as `client_secret_google.json`.

This app requests:

- `drive.file`, to create and update the visible ChatGPT folder/files
- `drive.appdata`, to store hidden rotating tokens and sync state

### 3. Install locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4. Authorize Strava and Google

```powershell
python -m garmin_drive auth-strava
python -m garmin_drive auth-google
```

### 5. Bootstrap hidden Drive state

```powershell
python -m garmin_drive bootstrap-appdata
```

This uploads your local Strava token into Google Drive's hidden `appDataFolder`. From this point on, GitHub Actions can keep the token fresh without your PC being on.

### 6. Test using the same backend as GitHub Actions

```powershell
python -m garmin_drive sync-strava --state-backend drive --days 30 --enrich missing --publish-raw
```

For a full backfill:

```powershell
.\scripts\backfill_full_history.ps1 -Days 3650 -RequestBudget 900
```

The backfill is resumable. It caches raw archive files under `.data/raw_archive/`, stores sync metadata in hidden Drive app data, and skips raw files already listed in the hidden Drive manifest. If Strava's daily read limit is reached, rerun the same command after midnight UTC.

## GitHub Actions Setup

Push this repo to GitHub, then add these repository secrets:

| Secret | Required | Value |
| --- | --- | --- |
| `STRAVA_CLIENT_ID` | yes | Strava app client ID |
| `STRAVA_CLIENT_SECRET` | yes | Strava app client secret |
| `GOOGLE_TOKEN_JSON` | yes | Contents of `.data/tokens/google_token.json` |
| `GOOGLE_DRIVE_FOLDER_ID` | no | Existing output folder ID, only if the app can access it |
| `STRAVA_TOKEN_JSON_BOOTSTRAP` | no | Contents of `.data/tokens/strava_token.json`, only if you skip `bootstrap-appdata` |

The included workflow lives at `.github/workflows/sync-runs.yml` and runs every 4 hours:

```yaml
schedule:
  - cron: "17 */4 * * *"
```

The `17` minute avoids the top of the hour, when scheduled GitHub Actions jobs are more likely to be delayed.

You can also run it manually from GitHub's Actions tab. For the first manual run, use:

- `days`: `3650`
- `max_pages`: `25`
- `force_upload`: `true`
- `request_budget`: `900`

After that, the scheduled default of 14 days is enough to catch new runs and recent edits.

## ChatGPT Setup

Connect Google Drive in ChatGPT settings and choose the sync option. Ask ChatGPT to use the generated Drive folder.

Example prompt:

```text
Use my Google Drive run history folder. Summarize my last 8 weeks of running, identify mileage trend, long-run progression, average pace changes, and any signs I am stacking too much intensity.
```

## Local Commands

```powershell
python -m garmin_drive auth-strava
python -m garmin_drive auth-google
python -m garmin_drive bootstrap-appdata
python -m garmin_drive sync-strava --state-backend drive --days 30 --enrich missing --publish-raw
.\scripts\backfill_full_history.ps1 -Days 3650 -RequestBudget 900
```

Use `--no-upload` to update local output and hidden state without publishing visible Drive files.

Use `--dry-run` to inspect fetched runs without writing.

Use `--force-upload` if you need to rebuild the visible Drive folder even when the hidden run-history digest says nothing changed.

Use `--enrich none`, `--enrich missing`, or `--enrich full` to control detailed Strava activity/stream fetching. `missing` is the default and is what scheduled GitHub Actions uses.

Use `--recent-mile-days 14` to control how much mile-by-mile HR/elevation detail appears in top-level compact files. Full sample streams remain in `Raw Data`.

Use `--request-budget 900` to cap Strava API calls during a run. The app watches Strava rate-limit headers and sleeps through short 15-minute read limits when useful.

To include different activity types, edit `STRAVA_ACTIVITY_SPORT_TYPES` in `.env` and the matching GitHub Actions environment value. Strava sport types are case-sensitive, for example `Run`, `TrailRun`, `Ride`, and `VirtualRide`.
