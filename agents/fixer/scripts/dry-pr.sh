#!/usr/bin/env bash
# Render the PR body without opening anything.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

EXP_ID="${1:?usage: dry-pr.sh <experiment_id>}"
python -m agents.fixer.pr_template --experiment-id "${EXP_ID}"
