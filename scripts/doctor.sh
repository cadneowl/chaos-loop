#!/usr/bin/env bash
# Diagnose the state of the local stack. Exit code = number of failed checks.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

failures=0
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '\033[1;32m  ok\033[0m  %s\n' "$label"
  else
    printf '\033[1;31m fail\033[0m  %s\n' "$label"
    failures=$((failures + 1))
  fi
}

printf '\033[1m# host tools\033[0m\n'
check "docker"          docker info
check "kubectl"         command -v kubectl
check "kind"            command -v kind
check "helm"            command -v helm
check "python (3.11+)"  bash -c 'python3 --version | awk "{print \$2}" | awk -F. "{exit !(\$1>=3 && \$2>=11)}"'
check ".venv exists"    test -d .venv

printf '\n\033[1m# kind cluster\033[0m\n'
check "cluster 'chaos' exists" bash -c 'kind get clusters | grep -qx chaos'
check "kube-context kind-chaos" bash -c 'kubectl config current-context | grep -qx kind-chaos'

printf '\n\033[1m# chaos-mesh\033[0m\n'
check "ns chaos-mesh"      kubectl get ns chaos-mesh
check "controller ready"   kubectl -n chaos-mesh wait --for=condition=available --timeout=5s deploy/chaos-controller-manager
check "CRDs present"       bash -c 'kubectl get crd | grep -q chaos-mesh.org'

printf '\n\033[1m# observability\033[0m\n'
check "ns observability"   kubectl get ns observability
check "prometheus ready"   kubectl -n observability wait --for=condition=available --timeout=5s deploy/kps-kube-prometheus-stack-prometheus

printf '\n\033[1m# target app\033[0m\n'
check "ns otel-demo"               kubectl get ns otel-demo
check "namespace annotation set"   bash -c 'kubectl get ns otel-demo -o jsonpath="{.metadata.annotations.chaos\.kosta\.dev/allowed}" | grep -qx true'
check "pods ready"                 kubectl -n otel-demo wait --for=condition=ready --timeout=5s pod --all

printf '\n\033[1m# security tools\033[0m\n'
for t in trivy syft grype gitleaks cosign kubescape; do
  check "$t installed"  command -v "$t"
done
check "ZAP image pulled" bash -c 'docker image inspect owasp/zap2docker-stable >/dev/null'

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[1;32mall clear\033[0m — %s\n' "$(date)"
else
  printf '\033[1;33m%d check(s) failed\033[0m\n' "$failures"
fi
exit "$failures"
