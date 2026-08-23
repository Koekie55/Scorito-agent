param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonPath)) {
    throw "Python was not found: $PythonPath"
}

$entryPoint = Join-Path $ProjectRoot "scripts\evaluate_gt_stage.py"
$logDirectory = Join-Path $ProjectRoot "data\stage_evaluation_logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$logPath = Join-Path $logDirectory ((Get-Date -Format "yyyyMMdd-HHmmss") + ".log")

& $PythonPath $entryPoint --refresh-results --send-teams-if-configured *>> $logPath
if ($LASTEXITCODE -ne 0) {
    throw "Grand Tour stage evaluation failed with exit code $LASTEXITCODE. See $logPath"
}