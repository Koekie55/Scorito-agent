param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "",
    [string[]]$Recipients = @("quintenkoe@hotmail.com", "wouterjanson@gmail.com")
)

$ErrorActionPreference = "Stop"
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonPath)) {
    throw "Python was not found: $PythonPath"
}

$analysis = Join-Path $ProjectRoot "scripts\analyze_vuelta_model_performance.py"
& $PythonPath $analysis --simulations 10000 --seed 20260909
if ($LASTEXITCODE -ne 0) {
    throw "Model-performance analysis failed with exit code $LASTEXITCODE."
}

$bodyPath = Join-Path $ProjectRoot "data\scorito\vuelta2026\email\model_performance_by_stage.html"
if (-not (Test-Path $bodyPath)) {
    throw "Model-performance email body was not found: $bodyPath"
}

$outlook = New-Object -ComObject Outlook.Application
$mail = $null
try {
    $mail = $outlook.CreateItem(0)
    foreach ($address in $Recipients) {
        if (-not $address) { continue }
        $recipient = $mail.Recipients.Add($address)
        if ($null -eq $recipient) {
            throw "Outlook could not add recipient: $address"
        }
    }
    if (-not $mail.Recipients.ResolveAll()) {
        throw "Outlook could not resolve all recipients."
    }
    $mail.Subject = "[Scorito Vuelta] Model performance by stage and Monte Carlo backtest"
    $mail.HTMLBody = Get-Content -Raw -Encoding UTF8 $bodyPath
    $mail.Send()
    Write-Output "Sent model-performance overview to: $($Recipients -join ', ')"
}
finally {
    if ($null -ne $mail) {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($mail)
    }
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($outlook)
}