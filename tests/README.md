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

These are the milestone-0 tests. As real agents land, each gets its own folder with integration tests that may require a kind cluster or recorded fixtures.
