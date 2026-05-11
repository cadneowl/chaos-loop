#!/usr/bin/env bash
# Install kube-prometheus-stack + Loki + Tempo via helm.
set -euo pipefail

NS="${NS:-observability}"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update >/dev/null

kubectl create namespace "${NS}" --dry-run=client -o yaml | kubectl apply -f -

# Metrics + Grafana
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  --namespace "${NS}" \
  --set grafana.service.type=NodePort \
  --set grafana.service.nodePort=30080 \
  --set prometheus.service.type=NodePort \
  --set prometheus.service.nodePort=30090 \
  --wait

# Logs
helm upgrade --install loki grafana/loki-stack \
  --namespace "${NS}" \
  --set promtail.enabled=true \
  --wait

# Traces
helm upgrade --install tempo grafana/tempo \
  --namespace "${NS}" \
  --wait

echo "Observability stack installed; Grafana on :30080, Prometheus on :30090."
echo "Default Grafana password: kubectl -n ${NS} get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d"
