#!/usr/bin/env bash
# Halt a running experiment and force-clean all Chaos Mesh CRDs in the target ns.
# Usage: scripts/abort.sh [namespace]  (defaults to otel-demo)
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

NS="${1:-otel-demo}"

say() { printf '\033[1;36m[abort]\033[0m %s\n' "$*"; }

# Tell the orchestrator to stop (milestone 1 will wire this command).
if [ -d .venv ]; then
  source .venv/bin/activate
  say "telling orchestrator to abort current experiments..."
  chaos abort --all 2>/dev/null || say "  (chaos abort not yet implemented; falling through to manual cleanup)"
fi

# Force-delete Chaos Mesh CRDs in the target namespace.
say "deleting all Chaos Mesh resources in namespace '${NS}'..."
for kind in podchaos networkchaos iochaos stresschaos dnschaos httpchaos timechaos kernelchaos; do
  kubectl -n "${NS}" delete "${kind}.chaos-mesh.org" --all --ignore-not-found
done

say "done. Verify with: kubectl get -n ${NS} all"
