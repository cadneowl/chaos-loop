#!/usr/bin/env bash
# Stop port-forwards. Leaves the kind cluster running so you keep state.
set -euo pipefail

say() { printf '\033[1;36m[stop]\033[0m %s\n' "$*"; }

# Kill any kubectl port-forward we started
if pgrep -f "kubectl.*port-forward" >/dev/null 2>&1; then
  pkill -f "kubectl.*port-forward"
  say "port-forwards stopped"
else
  say "no port-forwards running"
fi

say "cluster is still up. Use scripts/clean.sh to fully destroy it."
