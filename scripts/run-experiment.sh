#!/usr/bin/env bash
# Convenience wrapper: activate venv, run an experiment.
# Usage: scripts/run-experiment.sh experiments/examples/01-redis-network-loss.yaml [--dry-run]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <plan.yaml> [chaos run flags...]" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "no .venv found — run scripts/install.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

exec chaos run "$@"
