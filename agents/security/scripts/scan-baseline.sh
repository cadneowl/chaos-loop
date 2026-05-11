#!/usr/bin/env bash
# Run a full security baseline scan against a namespace.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

NS="${1:-otel-demo}"
python -m agents.security.agent baseline --namespace "${NS}"
