# Bring the local stack up. Windows variant.
$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Say($msg) { Write-Host "[start] $msg" -ForegroundColor Cyan }

$existing = & kind get clusters 2>$null
if (-not ($existing -contains "chaos")) {
    Say "cluster 'chaos' missing — run scripts\install.ps1 first"; exit 1
}
kubectl config use-context kind-chaos | Out-Null

Say "waiting for Chaos Mesh..."
kubectl -n chaos-mesh wait --for=condition=available deploy --all --timeout=120s

Say "waiting for observability..."
try { kubectl -n observability wait --for=condition=available deploy --all --timeout=120s } catch {}

# Kill any existing kubectl port-forwards
Get-Process -Name kubectl -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*port-forward*"
} | Stop-Process -Force

New-Item -ItemType Directory -Path ".cache\portforward" -Force | Out-Null

Say "port-forwarding grafana :3000"
Start-Process -WindowStyle Hidden kubectl -ArgumentList "-n","observability","port-forward","svc/kps-grafana","3000:80" `
    -RedirectStandardOutput ".cache\portforward\grafana.log" -RedirectStandardError ".cache\portforward\grafana.err"

Say "port-forwarding prometheus :9090"
Start-Process -WindowStyle Hidden kubectl -ArgumentList "-n","observability","port-forward","svc/kps-kube-prometheus-stack-prometheus","9090:9090" `
    -RedirectStandardOutput ".cache\portforward\prometheus.log" -RedirectStandardError ".cache\portforward\prometheus.err"

Say "port-forwarding chaos dashboard :2333"
Start-Process -WindowStyle Hidden kubectl -ArgumentList "-n","chaos-mesh","port-forward","svc/chaos-dashboard","2333:2333" `
    -RedirectStandardOutput ".cache\portforward\dashboard.log" -RedirectStandardError ".cache\portforward\dashboard.err"

Say "port-forwarding otel-demo :8080"
Start-Process -WindowStyle Hidden kubectl -ArgumentList "-n","otel-demo","port-forward","svc/otel-demo-frontendproxy","8080:8080" `
    -RedirectStandardOutput ".cache\portforward\frontend.log" -RedirectStandardError ".cache\portforward\frontend.err"

Start-Sleep -Seconds 2
Say "ready."
Say "  - Grafana            http://localhost:3000"
Say "  - Prometheus         http://localhost:9090"
Say "  - Chaos Dashboard    http://localhost:2333"
Say "  - OTel Demo          http://localhost:8080"
