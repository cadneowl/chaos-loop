#!/usr/bin/env bash
# TraceQL search within an experiment's chaos window.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

EXP_ID="${1:?usage: query-tempo.sh <experiment_id> '<traceql>'}"
QUERY="${2:?usage: query-tempo.sh <experiment_id> '<traceql>'}"
python -m agents.diagnostician.tools tempo --experiment-id "${EXP_ID}" --query "${QUERY}"
