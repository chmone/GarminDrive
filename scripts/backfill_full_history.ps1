param(
    [int]$Days = 3650,
    [int]$RequestBudget = 900,
    [int]$MaxPages = 25,
    [int]$RecentMileDays = 14,
    [switch]$ForceUpload
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

$SyncArgs = @(
    "-m", "garmin_drive",
    "sync-strava",
    "--state-backend", "drive",
    "--days", "$Days",
    "--max-pages", "$MaxPages",
    "--enrich", "missing",
    "--publish-raw",
    "--recent-mile-days", "$RecentMileDays",
    "--request-budget", "$RequestBudget"
)

if ($ForceUpload) {
    $SyncArgs += "--force-upload"
}

Write-Host "Running full Strava backfill with request budget $RequestBudget..."
& $Python @SyncArgs
