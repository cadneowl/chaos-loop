# tests

Unit tests that run without a kubernetes cluster or any real LLM calls.

```bash
# One-time setup
uv venv
uv pip install -e ".[dev]"

# Run
pytest tests/
```

## What's covered

- `test_contracts.py` — every example YAML loads into `ExperimentPlan`; budget validates; `FixProposal.is_draft` invariant
- `test_safety.py` — every safety gate
- `test_dry_run_loop.py` — full orchestrator loop end-to-end against mock agents
- `test_plugins_*.py` — experiment-plugin lifecycle, host, registry, examples

### Regression suites

- `test_regression_scenario.py` — suite YAML load/dump, `oracle_defaults` merge, validation (bad fault / journey refs), stable `suite_id`
- `test_playwright_oracle.py` — Playwright JSON parse + newly-failing delta, `--grep` derivation, dry-run stub, unassessable baseline
- `test_command_oracle.py` — exit-code oracle delta
- `test_suite_runner.py` — outcome classification (PASS/REGRESSED/BASELINE_FAIL/ERROR), persistence, oracle-authoritative verdict, progress callback
- `test_regression_coverage.py` — coverage matrix (covered/gap/unknown), category-scoped axis, `n/a` comprehensiveness
- `test_regression_relevance.py` — footprint sources (declarative + trace) and evidence-backed `n-a` classification
- `test_regression_drift.py` — chronic drift: baseline_passing extraction, golden diff, golden storage round-trip

These run without a cluster or LLM. Live-cluster integration is exercised by the scripts in `scripts/`, not here.
