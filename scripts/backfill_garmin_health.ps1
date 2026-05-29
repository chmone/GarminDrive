param(
    [Parameter(Mandatory = $true)]
    [string]$StartDate,
    [string]$EndDate = (Get-Date).ToString("yyyy-MM-dd"),
    [int]$ChunkDays = 30,
    [int]$PauseSeconds = 10,
    [switch]$ForceRefetch,
    [switch]$ForceUpload,
    [switch]$NoUpload
)

$ErrorActionPreference = "Stop"

if ($ChunkDays -lt 1) {
    throw "ChunkDays must be at least 1."
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

$Start = [DateTime]::ParseExact($StartDate, "yyyy-MM-dd", $null)
$End = [DateTime]::ParseExact($EndDate, "yyyy-MM-dd", $null)
if ($End -lt $Start) {
    throw "EndDate must be on or after StartDate."
}

$Current = $Start
$ChunkNumber = 1
while ($Current -le $End) {
    $ChunkEnd = $Current.AddDays($ChunkDays - 1)
    if ($ChunkEnd -gt $End) {
        $ChunkEnd = $End
    }

    $From = $Current.ToString("yyyy-MM-dd")
    $To = $ChunkEnd.ToString("yyyy-MM-dd")
    Write-Host "Garmin health backfill chunk $ChunkNumber`: $From to $To"

    $BackfillArgs = @(
        "-m", "garmin_drive",
        "backfill-garmin-health",
        "--state-backend", "drive",
        "--start-date", $From,
        "--end-date", $To
    )

    if ($ForceRefetch) {
        $BackfillArgs += "--force-refetch"
    }
    if ($ForceUpload) {
        $BackfillArgs += "--force-upload"
    }
    if ($NoUpload) {
        $BackfillArgs += "--no-upload"
    }

    & $Python @BackfillArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Garmin health backfill failed for $From to $To with exit code $LASTEXITCODE."
    }

    $Current = $ChunkEnd.AddDays(1)
    $ChunkNumber += 1
    if ($Current -le $End -and $PauseSeconds -gt 0) {
        Start-Sleep -Seconds $PauseSeconds
    }
}

Write-Host "Garmin health backfill complete: $StartDate to $EndDate"
