# target — the system being chaos-engineered

## Default target: OpenTelemetry Demo

We bundle the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) as the canonical victim. It's a polyglot microservices app (~16 services across Go, Python, Java, TypeScript, .NET, Ruby, C++) representing a synthetic e-commerce site. It's:

- Well-instrumented (the entire point of the demo) — Prometheus, Loki, Tempo all see it
- Multi-language → covers code-reading edge cases for the diagnostician / fixer
- Has known fragilities to discover (e.g., the `cartservice` hard-depends on Valkey/Redis)
- Maintained by the CNCF, broadly familiar, real-ish

### Install (milestone 1)

```bash
# Helm install into the chaos-allowed namespace (set up by infra/install-chaos-mesh.sh)
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update
helm upgrade --install otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo \
  --create-namespace

# Annotate namespace so chaos safety gate is satisfied (also done by install-chaos-mesh.sh)
kubectl annotate namespace otel-demo chaos.kosta.dev/allowed="true" --overwrite

# Wait for ready
kubectl -n otel-demo wait --for=condition=ready pod --all --timeout=300s
```

### Reference: where things are

- Source: https://github.com/open-telemetry/opentelemetry-demo
- Frontend: `:8080` on the frontend service (NodePort 30030 by default in our kind config)
- Critical dependencies for hypothesis-generation:
  - `cartservice` ↔ `valkey-cart` (Redis-compatible) — hard dep, no fallback in the default config
  - `accountingservice` ↔ Kafka — async, has retry
  - Various services ↔ OTel Collector — degrade-gracefully expected

## Pointing at your own app

The `target/` directory is **not load-bearing for the orchestrator**. The agents reference the target via `target_app` (string identifier) and `target_repo` (git URL) on every Pydantic request. You can:

1. Bring your own app — set `target_app` and `target_repo` in your experiment YAML.
2. Ensure your target namespace has `chaos.kosta.dev/allowed: "true"`.
3. Configure your observability stack to expose Prometheus, Loki, and (optionally) Tempo endpoints reachable from where the orchestrator runs.

## What lives in this folder

- `README.md` (this file)
- `install.sh` — wrapper script to deploy OTel Demo (milestone 1)
- `security-fixtures/` — known-vulnerable / unsigned images used by `image.swap_vuln` and `image.swap_unsigned` faults. **Synthetic only**; never real production CVE payloads.
- `hypotheses-seed.yaml` — a curated starter list of hypotheses for OTel Demo, useful before the tester agent is ready to generate its own.
