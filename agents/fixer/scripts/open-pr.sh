#!/usr/bin/env bash
# Take a proposal artifact and actually open a draft PR.
# Requires `gh auth login` and a designated fork.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

EXP_ID="${1:?usage: open-pr.sh <experiment_id>}"

command -v gh >/dev/null || { echo "gh CLI required"; exit 1; }
gh auth status >/dev/null || { echo "run: gh auth login"; exit 1; }

python -m agents.fixer.agent propose --experiment-id "${EXP_ID}" --open-pr
