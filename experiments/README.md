# experiments

Each experiment is one YAML file matching `shared.contracts.ExperimentPlan`. The orchestrator validates it on load — malformed YAML = run rejected before any action.

## Run an experiment

```bash
# Dry-run with mocked agents — proves the loop wiring without touching anything
chaos run experiments/examples/01-redis-network-loss.yaml --dry-run

# Real run (milestone 7+)
chaos run experiments/examples/01-redis-network-loss.yaml
```

## Inspect runs

```bash
chaos list                                            # recent
chaos show exp-abc123def456                           # one record as JSON
```

## Regression suites

`examples/regression/` holds a different kind of file: a **regression suite**
(`shared.contracts.RegressionSuite`, not `ExperimentPlan`). Where an experiment
*discovers* a weakness, a suite *replays* a curated corpus to confirm it stays
fixed, using the customer's own test suite as the oracle.

```bash
chaos regression coverage experiments/examples/regression/checkout.yaml --fault network.loss
chaos regression run      experiments/examples/regression/checkout.yaml --dry-run
```

Full guide: [`docs/REGRESSION.md`](../docs/REGRESSION.md).

## Layout

- `examples/` — curated reference experiments, committed
- `examples/regression/` — reference regression suites (RegressionSuite schema)
- `runs/` — output artifacts per experiment (gitignored)

## Authoring guidelines

- One fault per experiment in v1
- Always set `safety.namespace` and `safety.cluster_context`
- Always write a `rationale` for each fault — it's how future-you remembers *why* you ran this
- Link `hypothesis_id` to the seed list in `target/hypotheses-seed.yaml` where applicable
