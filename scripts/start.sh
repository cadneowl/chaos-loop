#!/usr/bin/env bash
# Bring the local stack up to a usable state. Idempotent.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

say() { printf '\033[1;36m[start]\033[0m %s\n' "$*"; }

# Cluster
if ! kind get clusters 2>/dev/null | grep -qx chaos; then
  say "cluster 'chaos' missing — run scripts/install.sh first"
  exit 1
fi
kubectl config use-context kind-chaos >/dev/null

# Wait for core components
say "waiting for Chaos Mesh controllers..."
kubectl -n chaos-mesh wait --for=condition=available deploy --all --timeout=120s

say "waiting for observability stack..."
kubectl -n observability wait --for=condition=available deploy --all --timeout=120s || true

# Port-forwards (background)
mkdir -p .cache/portforward
pkill -f "kubectl.*port-forward" 2>/dev/null || true
sleep 1

say "port-forwarding grafana :3000 -> localhost:3000"
nohup kubectl -n observability port-forward svc/kps-grafana 3000:80 \
  >.cache/portforward/grafana.log 2>&1 &

say "port-forwarding prometheus :9090 -> localhost:9090"
nohup kubectl -n observability port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 \
  >.cache/portforward/prometheus.log 2>&1 &

say "port-forwarding chaos-mesh dashboard :2333 -> localhost:2333"
nohup kubectl -n chaos-mesh port-forward svc/chaos-dashboard 2333:2333 \
  >.cache/portforward/dashboard.log 2>&1 &

say "port-forwarding otel-demo frontend :8080 -> localhost:8080"
nohup kubectl -n otel-demo port-forward svc/otel-demo-frontendproxy 8080:8080 \
  >.cache/portforward/frontend.log 2>&1 &

sleep 2

say "ready."
say "  - Grafana            http://localhost:3000  (admin / see install output)"
say "  - Prometheus         http://localhost:9090"
say "  - Chaos Dashboard    http://localhost:2333"
say "  - OTel Demo          http://localhost:8080"
