# `regression/` — resilience regression suites

The *confirmation* counterpart to the discovery loop. Replay a curated corpus of
frozen scenarios and assert everything that used to hold still holds — using the
customer's own test suite as the pass/fail **oracle**. Each scenario becomes an
`ExperimentPlan`, so it runs through the orchestrator loop and safety gates with
no changes to `loop.py`.

```
scenario.py            load/dump + validate suite YAML; stable suite_id from name
coverage.py            CoverageReporter — fault-by-journey matrix (covered / gap / n-a)
relevance.py           journey footprints -> evidence-backed n-a (declarative + trace sources)
drift.py               chronic axis: diff a fresh baseline against a stored golden
suite_runner.py        SuiteRunner — replay each scenario, classify the verdict, persist
oracles/
  playwright.py            inherit a Playwright project; newly-failing delta
  command.py               any exit-code command as the oracle
```

The oracles are ordinary experiment plugins (`chaos.plugins` entry points), so
they appear in `chaos plugins list`. The double-baseline lives in their
`capture_baseline` (clean) / `verify` (under fault) hooks — see
[`plugins/README.md`](../plugins/README.md).

Quick start:

```bash
chaos regression scaffold my-suite.yaml --suite-path ./e2e --target-app shop
chaos regression validate my-suite.yaml
chaos regression coverage my-suite.yaml --fault network.loss
chaos regression run      my-suite.yaml --dry-run
```

Full guide — model, oracle contract, coverage semantics, CLI, persistence — is in
[`docs/REGRESSION.md`](../docs/REGRESSION.md).
