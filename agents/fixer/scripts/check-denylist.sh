#!/usr/bin/env bash
# Check if a path is denied. Exit 0 = allowed, exit 1 = denied.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

PATH_ARG="${1:?usage: check-denylist.sh <path>}"
python -m agents.fixer.tools check-denylist --path "${PATH_ARG}"
