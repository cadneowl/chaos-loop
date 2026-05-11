#!/usr/bin/env bash
# Install everything needed to run chaos experiments locally.
# Idempotent: safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

say() { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Host prerequisites ---------------------------------------------------
for tool in docker kubectl helm; do
  command -v "$tool" >/dev/null || die "missing required tool: $tool"
done

if ! command -v kind >/dev/null; then
  say "installing kind..."
  go install sigs.k8s.io/kind@v0.23.0 2>/dev/null || {
    curl -sSfL https://kind.sigs.k8s.io/dl/v0.23.0/kind-$(uname)-amd64 -o /tmp/kind
    chmod +x /tmp/kind && sudo mv /tmp/kind /usr/local/bin/kind
  }
fi

# --- 2. Python venv + deps ---------------------------------------------------
say "setting up Python environment..."
if command -v uv >/dev/null; then
  uv venv .venv
  uv pip install -e ".[dev]"
else
  warn "uv not found; falling back to python -m venv + pip (slower)"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -e ".[dev]"
fi

# --- 3. Kind cluster ---------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx chaos; then
  say "kind cluster 'chaos' already exists"
else
  say "creating kind cluster 'chaos'..."
  kind create cluster --config infra/kind-cluster.yaml --name chaos
fi
kubectl config use-context kind-chaos

# --- 4. Chaos Mesh -----------------------------------------------------------
say "installing Chaos Mesh..."
bash infra/install-chaos-mesh.sh

# --- 5. Observability --------------------------------------------------------
say "installing observability stack..."
bash infra/observability/install.sh

# --- 6. Security tooling -----------------------------------------------------
say "installing security tooling..."
bash infra/security-tools/install.sh

# --- 7. Target app -----------------------------------------------------------
say "installing OpenTelemetry Demo as target..."
bash target/install.sh

say "done. Run 'bash scripts/doctor.sh' to confirm everything is healthy."
