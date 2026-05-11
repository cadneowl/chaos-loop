#!/usr/bin/env bash
# Generate a fix proposal artifact (patch + test + PR body) without opening a PR.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

EXP_ID="${1:?usage: propose.sh <experiment_id>}"
python -m agents.fixer.agent propose --experiment-id "${EXP_ID}" --no-pr
echo "artifacts: experiments/runs/${EXP_ID}/proposed/"
