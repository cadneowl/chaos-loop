#!/usr/bin/env bash
# Re-run scans and diff against the most recent baseline.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

NS="${1:-otel-demo}"
python -m agents.security.agent verify --namespace "${NS}"
