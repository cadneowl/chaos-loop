#!/usr/bin/env bash
# Find prior experiments similar to a given one (debug the similarity engine).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

EXP_ID="${1:?usage: similarity.sh <experiment_id>}"
python -m agents.diagnostician.similarity --experiment-id "${EXP_ID}"
