param(
    [Parameter(Mandatory = $true)]
    [string]$ActivityId,
    [ValidateSet("auto", "local", "drive")]
    [string]$StateBackend = "drive",
    [int]$RecentMileDays = 14,
    [switch]$DryRun,
    [switch]$NoUpload
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

$DeleteArgs = @(
    "-m", "garmin_drive",
    "delete-run",
    $ActivityId,
    "--state-backend", $StateBackend,
    "--recent-mile-days", $RecentMileDays
)

if ($DryRun) {
    $DeleteArgs += "--dry-run"
}
if ($NoUpload) {
    $DeleteArgs += "--no-upload"
}

& $Python @DeleteArgs
if ($LASTEXITCODE -ne 0) {
    throw "Delete run failed for activity $ActivityId with exit code $LASTEXITCODE."
}
