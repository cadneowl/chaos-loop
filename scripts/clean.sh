#!/usr/bin/env bash
# Nuke everything: cluster, venv, run database. Asks before doing anything.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

say() { printf '\033[1;36m[clean]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[clean]\033[0m %s\n' "$*"; }

warn "This will destroy:"
warn "  - kind cluster 'chaos' and everything in it"
warn "  - the .venv in this repo"
warn "  - experiment run database at \$HOME/.local/share/chaos/experiments.sqlite"
warn "  - port-forward caches"
printf "Type 'yes' to continue: "
read -r confirm
if [ "$confirm" != "yes" ]; then
  say "aborted"
  exit 1
fi

# Port-forwards
pkill -f "kubectl.*port-forward" 2>/dev/null || true

# Cluster
if kind get clusters 2>/dev/null | grep -qx chaos; then
  say "deleting kind cluster 'chaos'..."
  kind delete cluster --name chaos
fi

# venv
if [ -d .venv ]; then
  say "removing .venv"
  rm -rf .venv
fi

# Run db + caches
[ -f "$HOME/.local/share/chaos/experiments.sqlite" ] && rm -f "$HOME/.local/share/chaos/experiments.sqlite"
[ -d .cache ] && rm -rf .cache

say "clean."
