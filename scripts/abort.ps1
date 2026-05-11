param([string]$Namespace = "otel-demo")
$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Say($msg) { Write-Host "[abort] $msg" -ForegroundColor Cyan }

if (Test-Path .venv) {
    Say "telling orchestrator to abort..."
    try { & .\.venv\Scripts\python.exe -m orchestrator.main abort --all } catch { Say "  (chaos abort not yet implemented)" }
}

Say "deleting all Chaos Mesh resources in namespace '$Namespace'..."
foreach ($kind in @("podchaos","networkchaos","iochaos","stresschaos","dnschaos","httpchaos","timechaos","kernelchaos")) {
    kubectl -n $Namespace delete "$kind.chaos-mesh.org" --all --ignore-not-found
}
Say "done."
