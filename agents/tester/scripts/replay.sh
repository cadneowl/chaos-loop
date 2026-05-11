#!/usr/bin/env bash
# Replay the tester against a recorded fixture (no live cluster needed).
# Used in CI; uses VCR-style cassettes under agents/tester/tests/fixtures/.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

FIXTURE="${1:?usage: replay.sh <fixture-name>}"
python -m agents.tester.agent replay "agents/tester/tests/fixtures/${FIXTURE}.yaml"
