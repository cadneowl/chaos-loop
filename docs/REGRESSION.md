# Resilience regression suites

Where the closed loop (see [ARCHITECTURE.md](ARCHITECTURE.md)) *discovers* new
weaknesses one fault at a time, a **regression suite** *confirms* a curated
corpus: replay scenarios you already care about and assert everything that used
to hold still holds. Same machinery, inverted goal.

## Model

- **Scenario** — one frozen, replayable check: a `FaultSpec` + an oracle + the
  journeys it asserts. Internally it becomes an ordinary `ExperimentPlan`, so it
  runs through the same orchestrator loop and the same safety gates.
- **Suite** — an ordered set of scenarios plus `all_journeys` (the full journey
  denominator) and the non-prod `safety` block every scenario inherits.
- **Oracle** — the customer-owned pass/fail predicate, implemented as an
  experiment plugin. Two ship in v1:
  - `regression-playwright` — inherit a Playwright suite; the verdict is the
    set of *newly-failing* journeys.
  - `regression-command` — any exit-code command (pytest, a health check).
- **Coverage matrix** — fault (catalogue) × journey (the suite's), tri-state:
  `covered` / `gap` / `n-a`.

## The double baseline (why "newly-failing")

Resilience is differential, not absolute. Each scenario runs its oracle twice:

1. `capture_baseline` runs the suite **clean** (pre-fault) and records which
   journeys pass.
2. `verify` runs it again **under fault** and reports `green-at-baseline ∩
   red-now` — the journeys that *newly* failed.

A journey already red at baseline is a pre-existing failure, not a resilience
regression, so it can never be *newly* failing — the delta excludes it by
construction. If **every** journey a scenario asserts is red at baseline, the
scenario is *unassessable* and reports `BASELINE_FAIL` (the customer's suite is
broken, not the system under fault) rather than a misleading green `PASS` over
an empty delta. The orchestrator's separate, infra-level baseline-health gate
(built-in tester/security baseline) still surfaces a broken *environment* as an
abort. Flaky journeys are handled with the oracle's `retries` and a
`quarantine` list.

> Note: an unassessable scenario is detected in `verify`, so the fault is still
> injected before the verdict lands. Short-circuiting before injection would
> need a loop-level baseline hook — deferred, since it's an efficiency, not a
> correctness, concern.

## Verdict: the oracle is authoritative

The customer's oracle defines what "working" means, so a scenario's verdict is
driven by the oracle's `verify` result — **not** the built-in tester/security
signals (which are advisory here). This deliberately diverges from the discovery
loop's OR-of-all-signals rule: for a regression suite, the customer's suite is
the contract.

## Coverage semantics

The journey axis is the suite's `all_journeys`. The fault axis defaults to the
catalogue faults **in the categories the suite actually uses** — not the whole
catalogue — so a web suite's score isn't dragged down by hardware faults
(wifi/power/sensor) it will never run. Scope it explicitly with `--fault`.

A cell is `covered` when a scenario pairs that fault (by `FaultSpec.name`) with
that journey. Everything else is `unknown` and **counts as a gap** — v1 never
marks a cell `n-a` without evidence (trace-based relevance is a later milestone).

`comprehensiveness = covered / (covered + gaps)` — the *relevant* denominator
(NA excluded). It is `n/a` (not a misleading 100%) when there are no relevant
cells.

## CLI

```bash
# Enumerate a Playwright project's journeys into a starter suite.
chaos regression scaffold <out.yaml> --suite-path ./e2e [--list-json DUMP]

# Offline lint: bad fault names, journeys missing from all_journeys. Runs nothing.
chaos regression validate <suite.yaml>

# Render the coverage matrix (runs no scenarios; safe anywhere).
chaos regression coverage <suite.yaml> [--fault NAME ...]

# Replay every scenario, print verdicts + coverage. Exits non-zero on any regression.
#   --dry-run stubs the oracle (all journeys pass) to exercise the flow with no
#   Node / target — good for validating wiring end to end.
chaos regression run <suite.yaml> [--dry-run] [--profile static|hybrid|llm] [--db PATH]

# List recent suite runs, then drill into one.
chaos regression list [--db PATH]
chaos regression show <srun-id> [--db PATH]
```

Suites are validated on load — an unknown fault name or a journey missing from
`all_journeys` fails fast rather than silently misreporting as an uncovered gap.
Each scenario's run is auto-scoped to its own journeys (Playwright `--grep`), so
a big project isn't re-run in full for every scenario.

## Suite format

```yaml
name: checkout-resilience
target_app: otel-demo
target_repo: https://github.com/open-telemetry/opentelemetry-demo

safety:                 # every scenario inherits this (non-prod) block
  cluster_context: kind-chaos
  namespace: otel-demo

oracle: playwright      # default kind for every scenario
oracle_defaults:        # shallow-merged into each scenario's oracle_config
  suite_path: ./e2e
  base_url: http://localhost:8080
  retries: 2

all_journeys:           # the coverage denominator (invariant axis)
  - "checkout.spec.ts:add-to-cart"
  - "checkout.spec.ts:pay"
  - "browse.spec.ts:list-products"

scenarios:
  - title: checkout survives valkey-cart network loss
    fault:
      category: network
      name: network.loss          # a catalogue fault name
      target_selector: { app.kubernetes.io/component: valkey-cart }
      duration_seconds: 60
      rationale: cartservice depends on valkey-cart; verify graceful degradation
    journeys:
      - "checkout.spec.ts:add-to-cart"
      - "checkout.spec.ts:pay"
```

A worked example ships at
[`experiments/examples/regression/checkout.yaml`](../experiments/examples/regression/checkout.yaml).

## Persistence

Each suite run is stored as a `SuiteRunRecord` in the `suite_runs` table of the
same SQLite DB as experiments; every verdict links to the per-scenario
`ExperimentRecord` by `experiment_id`, so `chaos regression show` can drill into
full per-scenario detail.

## Scope (v1)

Shipped: inherit a suite → replay under a fault → newly-failing verdict →
coverage matrix (`covered` / `gap`). Read-only.

Later: trace-based relevance (`n-a` with evidence) and a drift axis; negative
"must-not-happen" assertions; connectors that ingest intent (Jira/GitLab) and
propose scenarios back.
