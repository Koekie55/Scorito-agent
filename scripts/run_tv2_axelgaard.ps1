param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "",
    [string]$RaceSlug = "vuelta-a-espana",
    [string]$Slug = "vuelta2026"
)

$ErrorActionPreference = "Stop"
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonPath)) {
    throw "Python was not found: $PythonPath"
}

$logDirectory = Join-Path $ProjectRoot "data\tv2_axelgaard\logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$logPath = Join-Path $logDirectory ((Get-Date -Format "yyyyMMdd-HHmmss") + "-tv2-axelgaard.log")

& $PythonPath (Join-Path $ProjectRoot "scripts\fetch_tv2_axelgaard.py") --race-slug $RaceSlug --slug $Slug *>> $logPath
if ($LASTEXITCODE -ne 0) {
    throw "TV 2 preview fetch failed with exit code $LASTEXITCODE. See $logPath"
}

# Re-validating after each fetch is what lets the earned weight grow as stages complete.
& $PythonPath (Join-Path $ProjectRoot "scripts\validate_tv2_axelgaard.py") --slug $Slug *>> $logPath
if ($LASTEXITCODE -ne 0) {
    "Validation failed; the previously earned weight stays in force." | Add-Content -Encoding UTF8 $logPath
}
Write-Output "TV 2 preview fetch complete. Log: $logPath"
