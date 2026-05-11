#!/usr/bin/env bash
# Run the tester in verify mode against the most recent baseline.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

TARGET="${1:-otel-demo}"
python -m agents.tester.agent verify --target "${TARGET}"
