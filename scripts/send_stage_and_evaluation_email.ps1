param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "",
    [string[]]$StageRecipients = @("quintenkoe@hotmail.com", "wouterjanson@gmail.com"),
    [string[]]$EvaluationRecipients = @("quintenkoe@hotmail.com")
)

$ErrorActionPreference = "Stop"
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonPath)) {
    throw "Python was not found: $PythonPath"
}

$builder = Join-Path $ProjectRoot "scripts\send_stage_and_evaluation_email.py"
& $PythonPath $builder
if ($LASTEXITCODE -ne 0) {
    throw "Email bodies could not be built (exit code $LASTEXITCODE)."
}

$emailDirectory = Join-Path $ProjectRoot "data\scorito\vuelta2026\email"
$messages = @(
    @{
        Path       = Join-Path $emailDirectory "stage_10_top20.html"
        Subject    = "[Scorito Vuelta] Stage 10 projected top 20 - corrected for abandons"
        Recipients = $StageRecipients
    },
    @{
        Path       = Join-Path $emailDirectory "model_evaluation.html"
        Subject    = "[Scorito Vuelta] Model evaluation and performance analysis after stage 9"
        Recipients = $EvaluationRecipients
    }
)
foreach ($message in $messages) {
    if (-not (Test-Path $message.Path)) {
        throw "Email body was not found: $($message.Path)"
    }
}

$outlook = New-Object -ComObject Outlook.Application
try {
    foreach ($message in $messages) {
        $mail = $outlook.CreateItem(0)
        try {
            foreach ($address in $message.Recipients) {
                if (-not $address) { continue }
                $recipient = $mail.Recipients.Add($address)
                if ($null -eq $recipient) {
                    throw "Outlook could not add recipient: $address"
                }
            }
            if (-not $mail.Recipients.ResolveAll()) {
                throw "Outlook could not resolve all recipients for: $($message.Subject)"
            }
            $mail.Subject = $message.Subject
            $mail.HTMLBody = Get-Content -Raw -Encoding UTF8 $message.Path
            $mail.Send()
            Write-Output "Sent '$($message.Subject)' to $($message.Recipients -join ', ')"
        }
        finally {
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($mail)
        }
    }
}
finally {
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($outlook)
}
