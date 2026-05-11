# Install everything needed to run chaos experiments locally (Windows).
$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Say($msg) { Write-Host "[install] $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "[install] $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "[install] $msg" -ForegroundColor Red; exit 1 }

# 1. Host prerequisites
foreach ($tool in @("docker","kubectl","helm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Die "missing required tool: $tool"
    }
}

if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
    Say "installing kind via winget..."
    winget install --id Kubernetes.kind --silent --accept-source-agreements --accept-package-agreements
}

# 2. Python venv + deps
Say "setting up Python environment..."
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv .venv
    uv pip install -e ".[dev]"
} else {
    Warn "uv not found; falling back to python -m venv + pip"
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip
    & .\.venv\Scripts\pip install -e ".[dev]"
}

# 3. Kind cluster
$existing = & kind get clusters 2>$null
if ($existing -contains "chaos") {
    Say "kind cluster 'chaos' already exists"
} else {
    Say "creating kind cluster 'chaos'..."
    kind create cluster --config infra/kind-cluster.yaml --name chaos
}
kubectl config use-context kind-chaos | Out-Null

# 4. Chaos Mesh (bash scripts run via WSL or git-bash; warn if neither)
Say "installing Chaos Mesh..."
if (Get-Command bash -ErrorAction SilentlyContinue) {
    bash infra/install-chaos-mesh.sh
    bash infra/observability/install.sh
    bash target/install.sh
} else {
    Warn "bash not found; run the .sh installers manually under WSL or git-bash, or port to .ps1."
}

# 5. Security tooling (native Windows)
Say "installing security tooling..."
& "$PSScriptRoot\..\infra\security-tools\install.ps1"

Say "done. Run 'powershell -File scripts\doctor.ps1' to confirm everything is healthy."
