<#
Registers Windows scheduled tasks so everything starts automatically after a
reboot/logon and is restarted if it exits. Run in an elevated PowerShell from
the repository root on the machine (or VPS) that hosts the MT5 terminal:

    powershell -ExecutionPolicy Bypass -File deploy\windows\install_tasks.ps1 -Role gateway
    powershell -ExecutionPolicy Bypass -File deploy\windows\install_tasks.ps1 -Role engine

  -Role gateway : only the MT5 gateway runs here; the engine is elsewhere (bridge backend)
  -Role engine  : the whole engine runs here next to the terminal (mt5 backend)

Secrets are read from .env in the repository root - they are never written
into the task definition.

The MetaTrader 5 terminal must be logged in and "Algo Trading" enabled. The
tasks run in the interactive user session because the terminal is a desktop
application; configure Windows auto-logon on a VPS so they start after reboot.
#>
param(
    [ValidateSet("gateway", "engine")] [string]$Role = "gateway",
    [string]$Python = ".venv\Scripts\python.exe"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$py = Join-Path $root $Python
if (-not (Test-Path $py)) { throw "Python not found at $py. Create the venv first: py -m venv .venv" }

function Register-Mt5Task([string]$Name, [string]$Arguments) {
    $action = New-ScheduledTaskAction -Execute $py -Argument $Arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "registered task: $Name ($Arguments)"
}

if ($Role -eq "gateway") {
    Register-Mt5Task "MT5Quant Gateway" "main.py bridge"
} else {
    Register-Mt5Task "MT5Quant Engine" "main.py run"
}

# Independent watchdog, every minute.
$wd = New-ScheduledTaskAction -Execute $py -Argument "scripts\watchdog.py --max-age 180" -WorkingDirectory $root
$every = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
if ($Role -eq "engine") {
    Register-ScheduledTask -TaskName "MT5Quant Watchdog" -Action $wd -Trigger $every -Force | Out-Null
    Write-Host "registered task: MT5Quant Watchdog"
}
Write-Host "Done. Start now with: Start-ScheduledTask -TaskName 'MT5Quant $((Get-Culture).TextInfo.ToTitleCase($Role))'"
