param(
    [switch]$SkipRuns,
    [switch]$SkipHealth,
    [int]$Chunk = 50
)

# One-time SQL backfill: mirror the full raw run + health history already stored in Google Drive
# into Postgres (Supabase). No Strava/Garmin re-fetch — it just downloads the raw archive JSON that
# GarminDrive already published and upserts the rich tables (run_details, run_streams, health_raw,
# health_intraday, current_status) plus the existing summaries. Idempotent: safe to re-run.
#
# Requires in your .env (same values your Render crons use):
#   DATABASE_URL          Supabase *session pooler* URI
#   GOOGLE_TOKEN_JSON     Google Drive OAuth token (to read the archives from Drive)
#   BODYCOMPASS_USER_ID   the user id to tag rows with (optional; defaults to "default")

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

# Soft check: DATABASE_URL may live only in .env (config.py loads it via python-dotenv), so don't
# hard-fail — just warn if it's neither in the shell nor the .env file.
$EnvFile = Join-Path $RepoRoot ".env"
$HasDbUrl = $env:DATABASE_URL -or ((Test-Path -LiteralPath $EnvFile) -and (Select-String -Path $EnvFile -Pattern "^\s*DATABASE_URL\s*=" -Quiet))
if (-not $HasDbUrl) {
    Write-Warning "DATABASE_URL not found in the environment or .env. The backfill will no-op if the SQL sink is disabled."
}

$BackfillArgs = @("-m", "garmin_drive", "backfill-sql", "--chunk", $Chunk)
if ($SkipRuns) { $BackfillArgs += "--skip-runs" }
if ($SkipHealth) { $BackfillArgs += "--skip-health" }

Write-Host "Running one-time SQL backfill (Drive -> Postgres). No Strava/Garmin re-fetch."
& $Python @BackfillArgs
if ($LASTEXITCODE -ne 0) {
    throw "SQL backfill failed with exit code $LASTEXITCODE."
}

Write-Host "SQL backfill complete."
