#!/usr/bin/env bash
# Install OpenTelemetry Demo as the canonical chaos target.
set -euo pipefail

NS="${NS:-otel-demo}"

helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null
helm repo update open-telemetry >/dev/null

helm upgrade --install otel-demo open-telemetry/opentelemetry-demo \
  --namespace "${NS}" \
  --create-namespace \
  --wait \
  --timeout 10m

kubectl annotate namespace "${NS}" chaos.kosta.dev/allowed="true" --overwrite

echo "OTel Demo installed in namespace ${NS}."
echo "Frontend: kubectl -n ${NS} port-forward svc/otel-demo-frontendproxy 8080:8080"
