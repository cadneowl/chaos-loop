#!/usr/bin/env bash
# Apply one fault out-of-band of the orchestrator. Use only for debugging.
# Usage: inject.sh experiments/examples/01-redis-network-loss.yaml
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

PLAN="${1:?usage: inject.sh <plan.yaml>}"

echo "WARNING: bypassing orchestrator safety gates. Use only with --dry-run targets."
read -r -p "Continue? (yes/no): " ok
[ "$ok" = "yes" ] || exit 1

python -m agents.chaos.agent inject "${PLAN}"
