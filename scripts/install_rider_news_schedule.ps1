param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "Scorito Rider News",
    [string]$PythonPath = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Output "Removed scheduled task '$TaskName'."
    exit 0
}

$runner = Join-Path $ProjectRoot "scripts\run_rider_news.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}
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

$powerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$ProjectRoot`" -PythonPath `"$PythonPath`""
$action = New-ScheduledTaskAction -Execute $powerShellPath -Argument $arguments -WorkingDirectory $ProjectRoot
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At "06:40"
    New-ScheduledTaskTrigger -Daily -At "11:30"
    New-ScheduledTaskTrigger -Daily -At "20:00"
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 90)
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description "Collect Vuelta rider news at 06:40, 11:30 and 20:00 local time; the CLI gates runs to race season."
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Write-Output "Installed '$TaskName' for 06:40, 11:30 and 20:00 local time."

