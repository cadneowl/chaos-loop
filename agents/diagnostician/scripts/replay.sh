#!/usr/bin/env bash
# Replay diagnosis against a recorded fixture.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

FIXTURE="${1:?usage: replay.sh <fixture-name>}"
python -m agents.diagnostician.agent replay "agents/diagnostician/tests/fixtures/${FIXTURE}.yaml"
