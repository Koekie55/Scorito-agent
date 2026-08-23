param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "",
    [string[]]$Recipients = @("quintenkoe@hotmail.com", "wouterjanson@hotmail.com"),
    [switch]$UseExistingReport
)

$ErrorActionPreference = "Stop"
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonPath)) {
    throw "Python was not found: $PythonPath"
}

$entryPoint = Join-Path $ProjectRoot "scripts\daily_vuelta_refresh.py"
if (-not (Test-Path $entryPoint)) {
    throw "Daily Vuelta entry point not found: $entryPoint"
}
$logDirectory = Join-Path $ProjectRoot "data\rider_news\logs"
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$logPath = Join-Path $logDirectory ((Get-Date -Format "yyyyMMdd-HHmmss") + "-daily-recommendation.log")
$reportPath = Join-Path $ProjectRoot "data\scorito\vuelta2026\daily_stage_recommendation.json"
$markdownPath = Join-Path $ProjectRoot "data\scorito\vuelta2026\daily_stage_recommendation.md"

if (-not $UseExistingReport) {
    & $PythonPath $entryPoint --require-cyclingoracle *>> $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "Daily Vuelta refresh failed with exit code $LASTEXITCODE. See $logPath"
    }
}
if (-not (Test-Path $reportPath) -or -not (Test-Path $markdownPath)) {
    throw "Daily Vuelta report files were not found. See $logPath"
}

$report = Get-Content -Raw -Encoding UTF8 $reportPath | ConvertFrom-Json
if ($report.status -ne "forward_recommendation") {
    "No forward recommendation is available; email skipped." | Add-Content -Encoding UTF8 $logPath
    exit 0
}
$stageNumber = $report.target_stage.stage_no
if (-not $report.sources.cyclingoracle) {
    throw "Stage $stageNumber WielerOrakel/CyclingOracle prediction is not published; email was not sent."
}
if (-not $Recipients -or @($Recipients | Where-Object { $_ }).Count -eq 0) {
    throw "At least one email recipient is required."
}

$outlook = $null
$mail = $null
try {
    $outlook = New-Object -ComObject Outlook.Application
    $mail = $outlook.CreateItem(0)
    foreach ($recipientAddress in $Recipients) {
        $recipient = $mail.Recipients.Add($recipientAddress)
        if ($null -eq $recipient) {
            throw "Outlook could not add recipient: $recipientAddress"
        }
    }
    if (-not $mail.Recipients.ResolveAll()) {
        throw "Outlook could not resolve all email recipients."
    }
    $mail.Subject = "[Scorito Vuelta] Stage $stageNumber personal and Hawktuah recommendations"
    $mail.Body = Get-Content -Raw -Encoding UTF8 $markdownPath
    $mail.Send()
    "Email sent to: $($Recipients -join ', ')" | Add-Content -Encoding UTF8 $logPath
}
catch {
    ($_ | Out-String) | Add-Content -Encoding UTF8 $logPath
    throw
}
finally {
    if ($null -ne $mail) {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($mail)
    }
    if ($null -ne $outlook) {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($outlook)
    }
}
