#!/usr/bin/env bash
# Generate hypotheses from a target repo. Heavy token cost — caches results.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

TARGET_REPO="${1:?usage: run-hypothesize.sh <git-url>}"
python -m agents.tester.agent hypothesize --target-repo "${TARGET_REPO}"
