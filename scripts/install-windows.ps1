# Start NetMon automatically on this Windows computer whenever you log in,
# running quietly in the background, and restart it if it ever stops.
#
#   Install:    powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#   Uninstall:  powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Uninstall
#
# Run it from the NetMon folder as your normal user (no admin needed).
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$TaskName = "NetMon"
$Dir = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "NetMon will no longer start automatically. Your devices, history and settings are still in $Dir."
    exit 0
}

$Pythonw = Join-Path $Dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    Write-Host "Set up NetMon first (README step 1): the .venv folder is missing." -ForegroundColor Red
    exit 1
}

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m netmon.main --lan" -WorkingDirectory $Dir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "NetMon restaurant network monitor" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 5
try {
    $Health = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -TimeoutSec 5
    Write-Host "NetMon is running and will start automatically when you log in." -ForegroundColor Green
    Write-Host "Dashboard: http://localhost:8000"
    Write-Host "Watching $($Health.devices) devices. Logs: $Dir\netmon.log"
    Write-Host "If Windows asks whether to allow Python on your network, choose Private networks so phones can open the dashboard."
} catch {
    Write-Host "NetMon didn't answer yet. Check $Dir\netmon.log for errors." -ForegroundColor Yellow
}
