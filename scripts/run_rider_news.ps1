param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
if (-not $PythonPath) {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $PythonPath = $venvPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Python was not found. Create .venv or pass -PythonPath."
        }
        $PythonPath = $pythonCommand.Source
    }
}

$entryPoint = Join-Path $ProjectRoot "scripts\rider_news.py"
if (-not (Test-Path $entryPoint)) {
    throw "Rider-news entry point not found: $entryPoint"
}
$logDirectory = Join-Path $ProjectRoot "data\rider_news\logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$logPath = Join-Path $logDirectory ((Get-Date -Format "yyyyMMdd-HHmmss") + ".log")

& $PythonPath $entryPoint --scheduled --email-if-configured --external-data-root $ProjectRoot *>> $logPath
$code = $LASTEXITCODE
if ($code -ne 0) {
    throw "Rider-news run failed with exit code $code. See $logPath"
}

$predictionEntryPoint = Join-Path $ProjectRoot "scripts\refresh_vuelta_stage_predictions.py"
& $PythonPath $predictionEntryPoint *>> $logPath
$code = $LASTEXITCODE
if ($code -ne 0) {
    throw "Vuelta stage-prediction refresh failed with exit code $code. See $logPath"
}

