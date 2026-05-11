#!/usr/bin/env bash
# Render one FaultSpec to CRD YAML on stdout. No apply.
# Usage: render.sh experiments/examples/01-redis-network-loss.yaml [fault_index]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

PLAN="${1:?usage: render.sh <plan.yaml> [fault_index]}"
IDX="${2:-0}"

python -m agents.chaos.agent render "${PLAN}" --index "${IDX}"
