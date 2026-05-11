#!/usr/bin/env bash
# Run the diagnostician against an existing experiment record.
# Usage: diagnose-run.sh exp-000000000001
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

EXP_ID="${1:?usage: diagnose-run.sh <experiment_id>}"
python -m agents.diagnostician.agent diagnose --experiment-id "${EXP_ID}"
