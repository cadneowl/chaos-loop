#!/usr/bin/env bash
# Force-delete every Chaos Mesh CRD in a namespace. Use after a crash.
set -uo pipefail
NS="${1:-otel-demo}"

for kind in podchaos networkchaos iochaos stresschaos dnschaos httpchaos timechaos kernelchaos; do
  kubectl -n "${NS}" delete "${kind}.chaos-mesh.org" --all --ignore-not-found
done
echo "cleanup complete in namespace ${NS}"
