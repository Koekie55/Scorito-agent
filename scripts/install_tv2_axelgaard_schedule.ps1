<#
.SYNOPSIS
    Install the daily TV 2 (Axelgaard) stage-preview fetch.
.DESCRIPTION
    Measured publication window for Vuelta 2026 stages 1-3: the preview is last
    edited between 17:04 and 22:39 UTC on the evening before the stage, and is
    sometimes corrected afterwards. The 23:10 trigger captures it as soon as it
    is final; the 06:20 trigger picks up overnight corrections before the
    06:40 rider-news run and the daily recommendation.
#>
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "Scorito TV2 Axelgaard Previews",
    [string]$PythonPath = "",
    [string]$RaceSlug = "vuelta-a-espana",
    [string]$Slug = "vuelta2026",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Output "Removed scheduled task '$TaskName'."
    exit 0
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

$runner = Join-Path $ProjectRoot "scripts\run_tv2_axelgaard.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}

$powerShellPath = (Get-Command powershell.exe -ErrorAction Stop).Source
$wscriptPath = (Get-Command wscript.exe -ErrorAction Stop).Source
$hiddenLauncher = Join-Path $ProjectRoot "scripts\run-hidden.vbs"
if (-not (Test-Path $hiddenLauncher)) {
    throw "Hidden launcher not found: $hiddenLauncher"
}
$psCommand = "`"$powerShellPath`" -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$ProjectRoot`" -PythonPath `"$PythonPath`" -RaceSlug `"$RaceSlug`" -Slug `"$Slug`""
$arguments = "`"$hiddenLauncher`" $psCommand"
$action = New-ScheduledTaskAction -Execute $wscriptPath -Argument $arguments -WorkingDirectory $ProjectRoot
$triggers = @(
    New-ScheduledTaskTrigger -Daily -At "23:10"
    New-ScheduledTaskTrigger -Daily -At "06:20"
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description "Fetch Emil Axelgaard's TV 2 stage previews at 23:10 and 06:20 local time and revalidate the star signal."
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Write-Output "Installed '$TaskName' for 23:10 and 06:20 local time."
