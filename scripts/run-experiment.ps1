param([Parameter(Mandatory=$true)] [string]$Plan, [Parameter(ValueFromRemainingArguments)] [string[]]$Extra)
$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot
if (-not (Test-Path .venv)) { Write-Error "no .venv — run scripts\install.ps1 first" }
& .\.venv\Scripts\python.exe -m orchestrator.main run $Plan @Extra
