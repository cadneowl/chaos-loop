#!/usr/bin/env bash
# Validate suppression YAML.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"

for f in agents/security/config/*-ignore.yaml; do
  [ -f "$f" ] || continue
  python -c "import yaml,sys; yaml.safe_load(open('$f'))" && echo "  ok  $f"
done
