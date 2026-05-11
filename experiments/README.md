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

## Layout

- `examples/` — curated reference experiments, committed
- `runs/` — output artifacts per experiment (gitignored)

## Authoring guidelines

- One fault per experiment in v1
- Always set `safety.namespace` and `safety.cluster_context`
- Always write a `rationale` for each fault — it's how future-you remembers *why* you ran this
- Link `hypothesis_id` to the seed list in `target/hypotheses-seed.yaml` where applicable
