param(
    [int]$RecentMileDays = 14,
    [switch]$TrashOldIdFiles,
    [switch]$KeepLocalOldIdFiles
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

$PublishArgs = @(
    "-m", "garmin_drive",
    "publish-cache",
    "--state-backend", "drive",
    "--force-upload",
    "--recent-mile-days", "$RecentMileDays"
)

if ($TrashOldIdFiles) {
    $PublishArgs += "--trash-id-only-raw"
}

if (-not $KeepLocalOldIdFiles) {
    $PublishArgs += "--cleanup-local-id-only-raw"
}

Write-Host "Publishing cached archive with date/name/id raw filenames..."
& $Python @PublishArgs
