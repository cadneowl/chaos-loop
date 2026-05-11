# infra — local stack

This directory has the manifests + scripts to stand up a local environment for running chaos experiments.

## What gets installed

```
┌─────────────────────────────────────────────────────────┐
│ kind cluster (one node, by default)                     │
├──────────────┬────────────────────┬─────────────────────┤
│ Chaos Mesh   │ Observability      │ Security tooling    │
│ (CRDs +      │ - Prometheus       │ - Trivy (CLI)       │
│  controllers │ - Grafana          │ - Syft (CLI)        │
│  + dashboard)│ - Loki             │ - Grype (CLI)       │
│              │ - Tempo            │ - gitleaks (CLI)    │
│              │ - OTel Collector   │ - cosign (CLI)      │
│              │                    │ - kubescape (CLI)   │
│              │                    │ - ZAP (Docker run)  │
└──────────────┴────────────────────┴─────────────────────┘
                                                ▲
                                                │
                              ┌─────────────────┴────────────────┐
                              │ target app: OpenTelemetry Demo   │
                              │ (see ../target/README.md)         │
                              └──────────────────────────────────┘
```

## Why kind, not k3d / minikube

- kind is the most ergonomic for ephemeral CI-like clusters
- Chaos Mesh supports kind explicitly
- We don't need anything kind doesn't give us

If you have a different k8s flavor you prefer, the manifests are vanilla — point `KUBECONFIG` at your cluster and skip `kind-cluster.yaml`.

## Quickstart (placeholder — milestone 1)

```bash
# 1. Create the cluster
kind create cluster --config infra/kind-cluster.yaml --name chaos

# 2. Install Chaos Mesh (helm)
bash infra/install-chaos-mesh.sh

# 3. Install observability (kube-prometheus-stack + Loki + Tempo via helm)
bash infra/observability/install.sh

# 4. Verify
kubectl get pods -A
kubectl get crds | grep chaos-mesh.org
```

Then deploy the target app:
```bash
bash ../target/install.sh
```

## Security tooling

Most of the security scanners run as CLIs from the host, not inside the cluster. The exception is ZAP, which we run as a Docker container against the target's externally-exposed endpoints.

See `infra/security-tools/install.sh` for installation instructions per scanner (most are `brew install` / `apt install` / `winget install`).

## What's NOT in here

- Production-grade tuning. This is a local dev environment.
- Cloud provider integration. Bring your own.
- Long-term storage of Prometheus / Loki data. Ephemeral by design.
