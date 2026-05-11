#!/usr/bin/env bash
# Run a LogQL query within an experiment's chaos window.
# Usage: query-loki.sh <experiment_id> '<logql>'
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

EXP_ID="${1:?usage: query-loki.sh <experiment_id> '<logql>'}"
QUERY="${2:?usage: query-loki.sh <experiment_id> '<logql>'}"
python -m agents.diagnostician.tools loki --experiment-id "${EXP_ID}" --query "${QUERY}"
