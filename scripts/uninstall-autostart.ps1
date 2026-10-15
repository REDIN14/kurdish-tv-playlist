# Removes the automatic start (and the firewall rule, if -AllowLan was used). The data folder is left alone.
$ErrorActionPreference = "SilentlyContinue"
Stop-ScheduledTask -TaskName "KurdishIPTV"
Unregister-ScheduledTask -TaskName "KurdishIPTV" -Confirm:$false
Remove-NetFirewallRule -DisplayName "Kurdish TV playlist"
Get-NetTCPConnection -LocalPort 8787 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Write-Host "Autostart removed."
