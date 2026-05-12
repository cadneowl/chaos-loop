# Contributing

<p align="center">
  <img src="docs/cast/the_group.png" alt="The cast" width="640" />
</p>

Meet the cast: [docs/CAST.md](docs/CAST.md). They have opinions.

Thanks for your interest. Read this before sending a PR.

## TL;DR

```bash
# fork + clone, then:
python -m venv .venv && source .venv/bin/activate  # POSIX
# .\.venv\Scripts\Activate.ps1                     # Windows PowerShell
pip install -e ".[dev]"
pytest -q && ruff check . && mypy agents/ shared/ orchestrator/
# all green? you're ready.
```

## Development setup

The dev box is Python 3.13 on Windows. CI runs Python 3.11 and 3.13 on
Ubuntu. If you have access to a Linux box and `gh` CLI, that's the
smoothest path. WSL works too — see the install section of
[README.md](README.md#installation).

### One-time

```bash
git clone https://github.com/cadneowl/chaos-loop.git
cd chaos-loop
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Every PR must pass

| Check | Command |
|---|---|
| Linter (ruff) | `ruff check .` |
| Strict type checker | `mypy agents/ shared/ orchestrator/` |
| Unit tests | `pytest tests/ -q` |

CI runs all three on every push and pull request. Don't open a PR
without running them locally first; review time is more expensive than
your CPU's time.

### Optional but recommended

- **Live cluster smoke test** if you touched the chaos agent or
  KubernetesClusterIO: see [README.md → Operating](README.md#operating-the-system)
  for `scripts/validate_renderers.py` and friends.
- **`pre-commit install`** if you want ruff to run before each commit
  (config not shipped — straightforward to add locally).

## Coding standards

### Style

- ruff config in `pyproject.toml` is authoritative.
- Line length: 110.
- No emoji in code or comments unless asked by an issue.
- No unused imports / unused vars / unused `# type: ignore`.

### Types

- mypy strict is on. The relaxations we accept are documented in
  `[tool.mypy]` and the `[[tool.mypy.overrides]]` sections of
  `pyproject.toml`. Don't add new relaxations without justification in
  the PR description.

### Tests

- Every behavioral change needs a test. Pure refactors that don't
  change semantics can argue for skipping.
- Prefer unit tests over integration. Live-cluster tests live in
  `scripts/` and run manually, not in CI.
- Test names should describe the property, not the implementation:
  `test_drift_detection_flips_when_sbom_changes`, not
  `test_compute_drift`.

### Comments

- Default to writing NO comments. Identifiers should describe what.
- Add a comment only when the WHY is non-obvious: a hidden invariant,
  a workaround for a known bug, behavior that would surprise a reader.
- Never explain WHAT the code does. Never reference current
  tasks / PRs / milestones — those rot.

### Commits

- Lower-case subject, no trailing period, present tense (`add` not
  `added`).
- Subject ≤ 72 chars. Use the body for details, not the subject.
- One logical change per commit. A "code review pass" commit is fine
  if the changes are themed; a "various fixes" commit is not.
- No bot-style trailers (`Generated-by:` etc.) — git already records
  the author.

Example:

```
M4.1: Syft + Grype + gitleaks + cosign + kubescape

Five scanner wrappers, each matching the Trivy template:
- Syft: ...
- Grype: ...
[etc.]
```

## Adding new pieces

### A new detector

1. Implement the `Detector` Protocol in
   `agents/tester/detectors/<your_detector>.py` — one `find(code) ->
   list[Issue]` method.
2. Add a `_DETECTOR_CONFIG` entry in `agents/tester/hypothesizer.py`
   mapping it to a catalogue fault.
3. Register in `agents/tester/detectors/__init__.py` `default_detectors()`.
4. Add tests in `tests/test_static_hypothesizer.py`.
5. Document in the table in `README.md` and `agents/tester/README.md`.

### A new fault

1. Add a `FaultDef` entry to `agents/chaos/faults/_meta.py` `CATALOGUE`.
2. Write a renderer function in
   `agents/chaos/faults/<category>.py`.
3. Register in `agents/chaos/faults/registry.py` `RENDERERS`.
4. Add tests in `tests/test_chaos_renderers.py`.
5. Validate against a live Chaos Mesh:
   `python scripts/validate_renderers.py --context <your-context>`.

### A new scanner

1. Add `agents/security/scanners/<scanner>.py`. Match the Trivy template:
   an async `scan_*` function + a `_parse_*` helper.
2. Wire into `ClaudeSecurityAgent` via a new `SecurityScanConfig` flag.
   Default the flag to OFF — every scanner is opt-in to avoid silent
   binary dependencies.
3. Tests: parser tests against canned bytes + end-to-end tests through
   `FixtureRunner`.

### A new LLM strategy

The Hypothesizer / Diagnoser / FixerStrategy seams already host four
implementations each (Fixture / Static / Hybrid / Claude). A fifth
implementation should:

- Satisfy the same Protocol.
- Be selectable via `--profile` in `orchestrator/main.py` and
  `_factory.py`.
- Not require new top-level dependencies in `pyproject.toml` unless
  truly necessary.

## What we won't merge

- Code that's not strict-mypy-clean.
- New top-level dependencies without justification in the PR description.
- Test changes that lower coverage of a critical path.
- Network calls or filesystem writes in unit tests.
- "Just for completeness" features without a use case.
- Anything that makes the safety gates configurable to a less-safe
  default (e.g., changing `require_namespace_annotation` default to
  `False`).

## Getting unblocked

- Open a [Discussion](https://github.com/cadneowl/chaos-loop/discussions)
  for design questions.
- Open an [Issue](https://github.com/cadneowl/chaos-loop/issues/new)
  for concrete bugs / features.
- For security, see [SECURITY.md](SECURITY.md).
