# Architecture

## The closed loop

```
        ┌──────────────────────────────────────────────────────────┐
        │                                                          │
        ▼                                                          │
  ┌─────────────┐    ┌──────────────┐    ┌──────────────┐          │
  │ tester      │    │ security     │    │ orchestrator │          │
  │ baseline    │───▶│ baseline     │───▶│ baseline ok? │──no──┐   │
  │ hypotheses  │    │ scan         │    │              │      │   │
  └─────────────┘    └──────────────┘    └──────┬───────┘      │   │
                                                │yes           │   │
                                                ▼              │   │
                                         ┌──────────────┐      │   │
                                         │ chaos        │      │   │
                                         │ inject one   │      │   │
                                         │ fault        │      │   │
                                         └──────┬───────┘      │   │
                                                ▼              │   │
                                         ┌──────────────┐      │   │
                                         │ tester +     │      │   │
                                         │ security     │      │   │
                                         │ post-verify  │      │   │
                                         └──────┬───────┘      │   │
                                                ▼              │   │
                                         ┌──────────────┐      │   │
                                         │ regression?  │──no──┤   │
                                         └──────┬───────┘      │   │
                                                │yes           │   │
                                                ▼              │   │
                                         ┌──────────────┐      │   │
                                         │ diagnostician│      │   │
                                         │ RCA          │      │   │
                                         └──────┬───────┘      │   │
                                                ▼              │   │
                                         ┌──────────────┐      │   │
                                         │ fixer        │      │   │
                                         │ draft PR     │      │   │
                                         └──────┬───────┘      │   │
                                                │              │   │
                                                ▼              │   │
                                         ┌──────────────┐      │   │
                                         │ record run,  │◀─────┘   │
                                         │ next         │          │
                                         │ experiment   │──────────┘
                                         └──────────────┘
```

## Why deterministic orchestrator

Claude is the cognitive layer, not the state machine. The orchestrator is plain Python because:
- State transitions need to be debuggable, reproducible, and testable
- Safety gates (blast radius, abort conditions, budget) must not depend on LLM judgment
- Cost — every loop iteration that wakes up an agent has token cost; the orchestrator decides when to wake them

Each agent is invoked through a thin adapter that:
1. Accepts a typed input (Pydantic model from `shared/`)
2. Spawns a Claude Agent SDK session with that agent's system prompt and tool surface
3. Returns a typed output, validated against the schema
4. Bubbles up any safety-relevant signals to the orchestrator

## Agent specifications

### tester (`agents/tester/`)

**Input:** `TesterRequest` — kind=baseline|verify, target identifier, run history reference
**Output:** `TesterReport` — steady-state confirmation OR anomaly list, plus generated hypotheses

**Capabilities:**
- Run unit / integration tests in the target repo
- Run Playwright suites against the target's UI
- Query Prometheus for SLI metrics, compute statistical baseline (mean, p95, variance) over N runs
- Tail Loki for error rate / log pattern drift
- Read the target's source code to generate hypotheses ("service X has a hard dep on Y; what if Y lags?")

**Statistical baseline:** baseline is **never** a single snapshot. The tester runs the probe set N times (default 5), records distributions, and post-chaos verification asks "is the post-chaos sample drawn from the same distribution?" — not "do the numbers match exactly?"

### chaos (`agents/chaos/`)

**Input:** `ExperimentPlan` — one or more `FaultSpec` entries, target, duration, ramp profile
**Output:** `ChaosTimeline` — precise timestamps of when each fault was injected/removed, raw Chaos Mesh status

**Capabilities:**
- Render Chaos Mesh CRDs (`PodChaos`, `NetworkChaos`, `IOChaos`, `StressChaos`, `DNSChaos`, `HTTPChaos`, `TimeChaos`, `KernelChaos`)
- Render security-flavored faults (see [SECURITY_CHAOS.md](SECURITY_CHAOS.md))
- Apply, observe, clean up
- Hard-fail if blast-radius gate from orchestrator rejects the plan

**Note on attribution:** v1 injects **one fault per experiment** with quiet windows on either side, so the diagnostician can attribute symptoms cleanly. Multi-fault experiments are v2.

### security (`agents/security/`)

**Input:** `SecurityRequest` — kind=baseline|verify|drift, target endpoints / images / repo refs
**Output:** `SecurityReport` — findings list (severity, CVE refs, evidence, scanner provenance)

**Scanners wrapped:**
- **DAST**: OWASP ZAP baseline scan against target's HTTP endpoints
- **SBOM**: Syft (generate) — captured before and after chaos to detect runtime drift
- **SCA**: Grype (CVEs against SBOM)
- **Image scan**: Trivy (image vulns, misconfigs)
- **Secrets**: gitleaks against target repo + chaos-induced log dumps (does the system leak secrets when failing?)
- **K8s posture**: kubescape (NSA/MITRE frameworks)
- **Signature verify**: cosign

**Security hypotheses:** the security agent also generates hypotheses ("under load, the auth fallback should not bypass MFA" — testable by overloading auth + DAST against /admin).

### diagnostician (`agents/diagnostician/`)

**Input:** `DiagnosisRequest` — failed `TesterReport` and/or `SecurityReport`, `ChaosTimeline`, target repo path, observability access
**Output:** `DiagnosisReport` — ranked root-cause hypotheses, each with evidence pointers, confidence, suggested fix-class

**Capabilities:**
- Query Loki by time window + labels (correlated to chaos timeline)
- Query Prometheus for anomalous series
- Fetch distributed traces from Tempo / Jaeger
- Read target source code (file tree, grep, file read)
- Read prior `ExperimentRecord`s for similar failures

**Hard rule:** the diagnostician outputs *hypotheses*, never asserts. The fixer decides whether to act.

### fixer (`agents/fixer/`)

**Input:** `DiagnosisReport`
**Output:** `FixProposal` — { action: code-patch | k8s-config | doc-only | none, PR URL (if applicable), confidence, reasoning }

**Capabilities:**
- Edit target source code
- Write a regression test that *would have caught* the original symptom
- Open a draft PR via `gh` with: explanation, evidence pointers, the regression test
- Generate a doc-only output ("this is working as intended; here's the fragility note") when the diagnosis points at a deliberate-but-fragile design

**Never auto-merges.** The fixer's contract is "produce reviewable artifacts," not "ship."

## Orchestrator state machine

See `orchestrator/loop.py` (stub) and `orchestrator/safety.py` for the full enumeration. Key states:

- `INITIALIZING` → `BASELINE` → `BASELINE_OK` | `BASELINE_FAIL`
- `BASELINE_OK` → `INJECT` → `INJECTED` | `INJECT_FAILED`
- `INJECTED` → `VERIFY` → `STEADY` | `REGRESSED`
- `REGRESSED` → `DIAGNOSE` → `DIAGNOSED`
- `DIAGNOSED` → `PROPOSE_FIX` → `FIX_PROPOSED` | `FIX_DECLINED`
- (any) → `ABORTED` (on blast-radius breach, SLO trip, budget exceeded, user kill)
- → `RECORDED` → `NEXT`

Transitions are persisted to SQLite after each step so a crash mid-experiment is recoverable.

## Safety

See [SAFETY.md](SAFETY.md). Summary:
- Default target: a local kind cluster, never production
- Blast radius: one namespace, one app, one fault type per experiment
- Abort on: SLO trip, baseline-already-red, budget exceeded, user kill
- Manual approval gate on: fault types marked `requires_approval: true` in the catalogue
- Fixer never auto-merges
