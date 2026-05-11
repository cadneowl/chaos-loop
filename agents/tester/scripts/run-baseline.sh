#!/usr/bin/env bash
# Run the tester in baseline mode standalone.
# Usage: agents/tester/scripts/run-baseline.sh [target_app] [run_count]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

TARGET="${1:-otel-demo}"
N="${2:-5}"

python -m agents.tester.agent baseline --target "${TARGET}" --runs "${N}"
