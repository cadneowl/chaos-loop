#!/usr/bin/env bash
# Cheap SBOM-only drift check.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

NS="${1:-otel-demo}"
python -m agents.security.agent drift --namespace "${NS}"
