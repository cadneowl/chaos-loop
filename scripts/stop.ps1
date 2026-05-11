$ErrorActionPreference = "SilentlyContinue"
function Say($msg) { Write-Host "[stop] $msg" -ForegroundColor Cyan }

$pf = Get-Process -Name kubectl -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*port-forward*"
}
if ($pf) { $pf | Stop-Process -Force; Say "port-forwards stopped" }
else { Say "no port-forwards running" }

Say "cluster is still up. Use scripts\clean.ps1 to fully destroy it."
