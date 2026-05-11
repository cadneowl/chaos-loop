# Safety

Chaos engineering can break things. This document lists the guardrails this repo enforces and the assumptions behind them.

## The four hard gates

These are **deterministic, code-enforced** in `orchestrator/safety.py`. They do not depend on LLM judgment.

### 1. Target-cluster gate

Every experiment names a target cluster context. The orchestrator refuses to run unless:
- The cluster has the annotation `chaos.kosta.dev/allowed: "true"` on the target namespace, **and**
- The cluster name does not match a configurable denylist (default: anything containing `prod`, `production`, `live`, `main`)

### 2. Blast-radius gate

Per experiment, hard caps:
- One namespace per experiment
- One fault type per experiment in v1 (multi-fault is v2 with explicit attribution support)
- Maximum N pods affected (default 1 for v1, configurable per fault type)
- Maximum duration (default 5 min for v1)
- No `cluster-wide` selectors

The `chaos/` agent must declare these caps in the `FaultSpec` and the orchestrator rejects any plan that exceeds them.

### 3. Steady-state precheck

If the baseline `TesterReport` or `SecurityReport` already shows regression, the experiment is **aborted before any fault is injected**. Chaos-testing a system that's already broken produces useless data and risks turning a yellow into a red.

### 4. Abort conditions (continuous)

While an experiment is running, the orchestrator polls for abort signals every 5s:
- Cluster-level SLO breach (configurable Prometheus query)
- User kill (via `chaos abort <experiment-id>` CLI)
- Budget exceeded (token spend or wall-clock)
- Cluster health crashloop (kube-state-metrics)

On any of these, the orchestrator immediately:
1. Tells the `chaos/` agent to remove all injected faults
2. Marks the experiment `ABORTED`
3. Persists the partial record

## Approvals

Some faults require interactive approval:
- Anything with `requires_approval: true` in the catalogue (see `agents/chaos/faults/_meta.py`)
- All security-flavored faults that resemble attacks (`egress.exfil_sim`, `runtime.tamper`, etc.)
- Any plan with `duration > 5min`
- Any plan exceeding the default pod cap

Approval modes:
- `interactive` — prompts on CLI
- `gh-issue` — files a GitHub issue and waits for a `/approve` comment
- `slack` — posts to a Slack channel (future)

## Fixer constraints

The fixer agent is **the only agent that can write to a real codebase**, and it has its own gates:
- Always opens PRs as **draft**, never marks ready-for-review
- PRs are tagged with `chaos-fixer-proposal`, `confidence-low|med|high`, and a link back to the experiment record
- Maximum N open fixer PRs per repo at any time (default: 3)
- Refuses to touch paths in a configurable denylist (default: `.github/`, `infra/`, `secrets/`, anything in CODEOWNERS for `@security-team`)
- Never `--force` pushes, never amends, never auto-merges

## Cost / token budget

Per experiment:
- Soft cap: 2 USD of model spend → warn
- Hard cap: 10 USD → abort and record

Per day (across all experiments):
- Configurable in `~/.config/chaos/budget.yaml`
- Defaults to 50 USD

The orchestrator tracks spend by intercepting Claude Agent SDK responses and recording usage.

## What this does NOT protect against

- Bugs in the target app that only surface under chaos (that's the *point*; we want those)
- A malicious operator submitting an evil experiment YAML — assume operator is trusted
- Faults in the chaos engine itself (Chaos Mesh has its own well-known footguns; read its security docs)
- The user authorizing prod chaos — at that point you're on your own
