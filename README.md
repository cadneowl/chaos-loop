# chaos — closed-loop, security-aware chaos engineering

**Status:** early scaffolding. APIs and folder layout will change.

## What this is

A multi-agent system that closes the loop on chaos engineering:

```
                  ┌──────────────────────────────────────────────┐
                  │              orchestrator                    │
                  │   deterministic state machine + safety gates │
                  └────────────────┬─────────────────────────────┘
                                   │
        ┌──────────┬───────────────┼───────────────┬──────────────┐
        ▼          ▼               ▼               ▼              ▼
    ┌────────┐ ┌────────┐    ┌──────────┐   ┌──────────────┐ ┌────────┐
    │ tester │ │ chaos  │    │ security │   │ diagnostician│ │ fixer  │
    └────────┘ └────────┘    └──────────┘   └──────────────┘ └────────┘
   baseline +   inject +     DAST / SBOM     RCA from logs   draft PR
   hypotheses   security     SCA / secrets   + traces +      + docs,
   from code    faults via   posture, sign   chaos timeline  never
   reading      Chaos Mesh   verification    + both reports  auto-merges
```

The orchestrator runs a steady-state → inject → verify → (if regressed) diagnose → propose-fix loop, with explicit safety gates, blast-radius limits, and a token/$ budget.

## Why this exists

Existing chaos tools (Chaos Mesh, Litmus, AWS FIS, Gremlin) handle the *inject* step. A human reads dashboards and decides what broke. **Nobody widely-adopted has the closed loop yet.** The closest prior art:

- **[ChaosEater](https://github.com/ntt-dkiku/chaos-eater)** (NTT, ASE 2025 NIER) — closest, but research-licensed, modifies k8s manifests only (no application-code PRs), no security dimension, GPT-4o-tuned, Skaffold-bound.
- **Harness AI Reliability Agent** — commercial, closed-source, recommends experiments and gives remediation guidance, no autonomous code PRs.
- **LitmusChaos + MCP** — chat-driven experiment trigger, not a closed loop.

See [docs/COMPARISON.md](docs/COMPARISON.md) for the full landscape.

## How this is different

1. **Application-code PRs**, not just config edits — the fixer agent opens a draft PR in the target repo with a proposed code change, a test that would have caught the regression, and reasoning. Defaults to draft, never auto-merges.
2. **Security Chaos Engineering is first-class** — security findings flow through the same loop as functional regressions. See [docs/SECURITY_CHAOS.md](docs/SECURITY_CHAOS.md).
3. **Multi-agent with hard contracts** — every agent has a Pydantic-typed input/output schema in `shared/`. Agents are swappable; non-Claude implementations welcome.
4. **Hypothesis-driven** — the tester reads code and dependencies to *generate* hypotheses ("the cart service hard-depends on Redis — what happens when Redis lags?"), rather than humans hand-writing YAML.
5. **Claude-native** via Claude Agent SDK, with permissive Apache 2.0 license.

## What works today

This is a scaffold. The contracts, folder layout, and intended architecture are real; the agent implementations are stubs that show their intended shape. See [docs/ROADMAP.md](docs/ROADMAP.md) for what's next.

## Repo layout

```
chaos/
├── shared/             Pydantic contracts — the inter-agent interface (most important file)
├── orchestrator/       Deterministic loop, safety gates, budget tracking, state persistence
├── agents/
│   ├── tester/         Functional baseline + hypothesis generation
│   ├── chaos/          Chaos Mesh wrapper + security-flavored faults
│   ├── security/       DAST, SBOM/SCA, image scan, secrets, k8s posture
│   ├── diagnostician/  RCA from logs + traces + reports + chaos timeline
│   └── fixer/          Draft PR + docs, never auto-merges
├── infra/              kind cluster, Chaos Mesh install, observability stack, security tools
├── target/             OpenTelemetry Demo bundled as the canonical victim
├── experiments/        Hypothesis specs (YAML) + run artifacts
└── docs/               Architecture, security chaos, safety, comparison, roadmap
```

## Quickstart

```bash
# macOS / Linux / WSL
bash scripts/install.sh         # one-time
bash scripts/start.sh           # every session
bash scripts/doctor.sh          # sanity check
bash scripts/run-experiment.sh experiments/examples/01-redis-network-loss.yaml --dry-run
```

```powershell
# Windows
powershell -File scripts\install.ps1
powershell -File scripts\start.ps1
powershell -File scripts\doctor.ps1
powershell -File scripts\run-experiment.ps1 experiments\examples\01-redis-network-loss.yaml --dry-run
```

See [scripts/README.md](scripts/README.md) for the full operational script set.

## Per-agent plans

Each agent has a detailed implementation plan that we will follow milestone-by-milestone:

- [Tester](agents/tester/README.md) — baseline, verify, hypothesize (milestone 2)
- [Chaos](agents/chaos/README.md) — Chaos Mesh + security-flavored faults (milestone 3)
- [Security](agents/security/README.md) — DAST, SBOM/SCA, image scan, secrets, posture (milestone 4)
- [Diagnostician](agents/diagnostician/README.md) — RCA from logs + traces + code (milestone 5)
- [Fixer](agents/fixer/README.md) — draft PR + regression test, never auto-merges (milestone 6)

Reconciliation: [docs/PROPOSITION_VS_BUILT.md](docs/PROPOSITION_VS_BUILT.md) — what we agreed vs what's on disk today.

## License

Apache 2.0 — see [LICENSE](LICENSE).
