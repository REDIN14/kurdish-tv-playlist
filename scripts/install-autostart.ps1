# Starts the playlist service automatically (hidden) every time you log in, and restarts it if it stops.
#
#   powershell -File scripts\install-autostart.ps1              # install + start now
#   powershell -File scripts\install-autostart.ps1 -AllowLan    # also open the port for phones/TVs on your home network
#                                                                 (needs an administrator PowerShell)
param(
    [switch]$AllowLan,
    [int]$Port = 8787
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run scripts\setup.ps1 first (the Python environment is missing)." }

$taskName = "KurdishIPTV"
$bind = if ($AllowLan) { "0.0.0.0" } else { "127.0.0.1" }   # localhost only unless the home network is explicitly wanted
$action = New-ScheduledTaskAction -Execute $python -Argument "-m kurdiptv serve --host $bind --port $Port" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Kurdish TV playlist service (http://localhost:$Port/playlist.m3u)" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Installed. The service now starts at every login: http://localhost:$Port/playlist.m3u"

if ($AllowLan) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw "-AllowLan needs an administrator PowerShell (right-click -> Run as administrator)." }
    Remove-NetFirewallRule -DisplayName "Kurdish TV playlist" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "Kurdish TV playlist" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
    $ips = Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2\d|3[01]))\.' } | Select-Object -ExpandProperty IPAddress
    foreach ($ip in $ips) { Write-Host "Other devices on your network can use: http://${ip}:$Port/playlist.m3u" }
}
