#!/usr/bin/env bash
# Install Chaos Mesh into the current kube-context using helm.
# Idempotent: re-runs are safe.
set -euo pipefail

NS="${NS:-chaos-mesh}"
CHART_VERSION="${CHART_VERSION:-2.7.0}"

helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null
helm repo update chaos-mesh >/dev/null

kubectl create namespace "${NS}" --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace "${NS}" \
  --version "${CHART_VERSION}" \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.create=true \
  --set dashboard.securityMode=true \
  --wait

# Annotate the target namespace so the safety gate is satisfied.
TARGET_NS="${TARGET_NS:-otel-demo}"
kubectl create namespace "${TARGET_NS}" --dry-run=client -o yaml | kubectl apply -f -
kubectl annotate namespace "${TARGET_NS}" chaos.kosta.dev/allowed="true" --overwrite

echo "Chaos Mesh installed in ${NS}; target namespace ${TARGET_NS} annotated."
