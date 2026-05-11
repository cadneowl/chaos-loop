$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Say($msg) { Write-Host "[clean] $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "[clean] $msg" -ForegroundColor Yellow }

Warn "This will destroy:"
Warn "  - kind cluster 'chaos'"
Warn "  - .venv in this repo"
Warn "  - experiments.sqlite under `$HOME\AppData\Local\chaos\"
Warn "  - .cache port-forward logs"
$confirm = Read-Host "Type 'yes' to continue"
if ($confirm -ne "yes") { Say "aborted"; exit 1 }

# Port-forwards
Get-Process -Name kubectl -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*port-forward*" } | Stop-Process -Force

# Cluster
$existing = & kind get clusters 2>$null
if ($existing -contains "chaos") { Say "deleting kind cluster..."; kind delete cluster --name chaos }

# venv
if (Test-Path .venv) { Say "removing .venv"; Remove-Item -Recurse -Force .venv }

# Db + caches
$dbPath = Join-Path $env:LOCALAPPDATA "chaos\experiments.sqlite"
if (Test-Path $dbPath) { Remove-Item -Force $dbPath }
if (Test-Path .cache) { Remove-Item -Recurse -Force .cache }

Say "clean."
