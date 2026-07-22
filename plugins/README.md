# `plugins/` — experiment lifecycle plugins

Customer-supplied hooks that own the app-specific scaffolding around an
experiment: provision an environment, prefill data, arrange a test, run **custom
validation**, and tear everything down — with **guaranteed teardown**. The
orchestrator keeps owning the state machine, safety gates, budget, and the fault.

```
base.py                ExperimentPlugin (the hook contract) + PluginContext + SteadyStateGuard
host.py                PluginSession — runs the lifecycle, guarantees teardown
registry.py            discovery: entry points (`chaos.plugins`) + local dir ($CHAOS_PLUGINS_DIR)
examples/
  keyvalue_scenario.py     minimal toy (in-memory KV store) — every hook, smallest form
  web_service_scenario.py  realistic template (deployment lifecycle, SLO verify, guard)
  _fakes.py                deterministic FakeCluster/FakeService so examples run offline
```

Quick start:

```bash
chaos plugins list                                  # what's discovered
chaos run experiments/examples/05-plugin-web-service.yaml --dry-run \
  --plugin example-web-service
```

Full guide — lifecycle, hook reference, discovery/packaging, testing, cookbook,
FAQ — is in [`docs/PLUGINS.md`](../docs/PLUGINS.md).

## Regression oracles are plugins too

The resilience regression suites (`regression/`) are built on this exact
contract: each oracle (`regression-playwright`, `regression-command`) is an
`ExperimentPlugin` that uses `capture_baseline` to measure steady state before a
fault and `verify` to report the *newly-failing* delta under it. They're
discovered through the same `chaos.plugins` entry-point group, so they show up in
`chaos plugins list`. See [`docs/REGRESSION.md`](../docs/REGRESSION.md).
