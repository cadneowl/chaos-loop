# Proposition vs what's built

A snapshot of what we agreed to build vs what's actually on disk as of milestone 0 (scaffold).

## Original proposition (from brainstorm)

- Tester agents establish a baseline of correct system state using unit tests, metrics, logs, Playwright; verify same after chaos.
- Chaos agents (wrapping Chaos Mesh) execute one or more chaos steps; emit a report.
- If broken, diagnostic agents look through logs (correlated to chaos actions) to identify the cause.
- Fixer agents address the issue, creating documentation / PRs.
- Loop continues.
- Each agent type lives in its own folder.
- Tech stack chosen together; agents are Claude-backed.
- Security: **DAST and supply chain analysis integrated into the chaos lifecycle** (added in follow-up).

## What's built (milestone 0 — scaffold)

### Real, working code

| Area | File(s) | Status |
|---|---|---|
| Inter-agent contracts | `shared/contracts.py` | **Real.** All 20+ Pydantic models, validators, enums. |
| Orchestrator state machine | `orchestrator/loop.py` | **Real.** Drives the full loop end-to-end via Protocol-typed agent adapters. |
| Safety gates | `orchestrator/safety.py` | **Real.** Cluster denylist, namespace annotation, blast radius, baseline-healthy, budget. |
| Budget tracker | `orchestrator/budget.py` | **Real.** Token spend + wall-clock. |
| SQLite persistence | `orchestrator/store.py` | **Real.** Save/load/list ExperimentRecord. |
| CLI | `orchestrator/main.py` | **Real.** `chaos run / list / show`. |
| Mock agents (dry-run) | `agents/_mocks.py` | **Real.** Drives the full loop without LLMs or a cluster. |
| Fault catalogue | `agents/chaos/faults/_meta.py` | **Real.** 22 entries — 10 classical Chaos Mesh + 12 security-flavored. |
| Example experiments | `experiments/examples/*.yaml` | **Real.** 3 examples, 1 functional + 2 security. |
| Infra install scripts | `infra/*.sh` | **Real.** Idempotent installers for cluster + Chaos Mesh + observability + security tools. |
| Test suite | `tests/*.py` | **Real.** Contract validation, safety gates, dry-run end-to-end. |
| Docs | `docs/*.md`, `README.md` | **Real.** Architecture, security chaos, safety, comparison, roadmap. |

### Stubs (signature only; bodies are NotImplementedError with milestone markers)

| Area | File(s) | Lands in |
|---|---|---|
| Tester Claude wiring | `agents/tester/agent.py` | Milestone 2 |
| Chaos Mesh CRD render/apply | `agents/chaos/agent.py` | Milestone 3 |
| Security scanner shell-outs | `agents/security/scanners/*.py` | Milestone 4 |
| Diagnostician Claude wiring | `agents/diagnostician/agent.py` | Milestone 5 |
| Fixer Claude wiring + gh PR | `agents/fixer/agent.py` | Milestone 6 |

### Where security shows up (per the follow-up ask)

- A dedicated `agents/security/` agent peer to `tester/` — runs DAST (OWASP ZAP), SBOM (Syft), SCA (Grype), image scan (Trivy), secrets (gitleaks), signatures (cosign), k8s posture (kubescape).
- `chaos/` catalogue extends Chaos Mesh native faults with 12 security-flavored faults — cert revoke/expire, TLS downgrade, auth outage/latency, secret rotation, vuln/unsigned image swap, IAM degrade, netpol regress, exfil simulation, runtime tamper.
- `SecurityReport` flows through the orchestrator alongside `TesterReport`; the diagnostician and fixer treat security findings the same as functional regressions.
- See `docs/SECURITY_CHAOS.md` for the full picture.

## What's deliberately NOT done yet

- No real Claude SDK calls (every agent.py has TODO markers and refuses with NotImplementedError outside `--dry-run`)
- No real Chaos Mesh CRD application
- No real scanner invocations
- No `gh pr create` integration
- No prior art comparison against ChaosEater past the docs read
- No commit — `git init` only

## Where we go next

The agent READMEs (added in this commit alongside scripts/) each contain a milestone-by-milestone plan keyed to the same numbering.
