# fixer agent — implementation plan

> **Role:** take a `DiagnosisReport`, decide whether to act, and produce a **draft** PR with the proposed fix + a regression test (or a docs-only output for `working-as-intended` cases). Never auto-merges.

## Current implementations

Four variants behind one `FixerStrategy` Protocol (`agents/fixer/strategy.py`):

| Implementation | How it works | Cost |
|---|---|---|
| `FixtureFixerStrategy` | predetermined `FixerOutput` (or async callback); tests + dry-run | $0 |
| `StaticFixerStrategy` | per-fix-class templates emit a structured proposal (reasoning + files_touched + sketched regression-test path); doesn't actually edit files | **$0** |
| `HybridFixerStrategy` | tries LLM first; falls back to Static if LLM raises or returns empty | $$ |
| `ClaudeFixerStrategy` | LiteLLM-backed; reads target code via MCP tools; emits a JSON proposal; writes an artifact to `experiments/runs/<exp>/proposed/edits.json` | $$$ |

The fixer **agent** (`ClaudeFixerAgent` in `agent.py`) is independent of the strategy — it owns the deterministic decision tree (low-confidence → NONE; working-as-intended → DOC_ONLY directly; otherwise → delegate to strategy) and the path-denylist enforcement that always runs after the strategy. A strategy that proposes denylisted paths gets its proposal turned into `action=NONE` with the violation in the reasoning.

### What "Static" means here

The static templates describe **what to change and why**, not **the exact diff**. They cover 10 fix classes (`missing-retry`, `missing-timeout`, `missing-circuit-breaker`, `missing-fallback`, `auth-control-gap`, `secret-handling`, `image-policy`, `config-change`, `test-gap`, `code-patch`). Each output is a structured work item a human reviewer can act on — useful for triage and for handing off to a human or LLM to actually write the diff. Real file edits + `gh pr create` is M6.x.b, not yet implemented.

## 1. Mission

The fixer is the **only agent that writes to a real codebase**. Every guardrail in this design hardens that fact:

- Default action: **draft PR** for human review
- Pydantic validator: `FixProposal.is_draft` raises if set False
- Path denylist enforced before any file edit
- Mandatory regression test in every code-change PR
- One PR per experiment, no follow-up commits, no force-push, no amend

## 2. Position in the loop

```
[diagnostician] ─► DiagnosisReport
                       │
                       ▼
                  [fixer]
                       │
                       ├─► top hypothesis confidence < 0.5 → action="none", explain
                       │
                       ├─► fix_class="working-as-intended" → doc-only PR
                       │
                       └─► otherwise → branch + patch + test + draft PR
                                     │
                                     ▼
                                FixProposal
                                     │
                                     ▼
                              orchestrator records, loop ends
```

## 3. Contract

### Input — `DiagnosisReport`

The fixer reads the top hypothesis. If multiple are tied within 0.1 confidence, it can describe the alternates in `reasoning`, but acts on the top one.

### Output — `FixProposal`

```python
class FixProposal(BaseModel):
    experiment_id: ExperimentId
    run_id: RunId
    action: FixAction               # CODE_PATCH | CONFIG_CHANGE | DOC_ONLY | NONE
    pr_url: str | None              # None iff action=NONE
    confidence: float
    reasoning: str                  # link to evidence; reviewer's primary read
    files_touched: list[str]
    regression_test_added: bool     # MUST be True for CODE_PATCH / CONFIG_CHANGE
    is_draft: bool = True           # contract enforces; setting False raises
    started_at: datetime
    finished_at: datetime | None
```

## 4. Tool surface

| Tool | Signature | Purpose |
|---|---|---|
| `read_target_code` | `(path, range?)` | Read |
| `edit_target_code` | `(path, patch)` | Apply a unified-diff patch in the local checkout (NOT the upstream repo) |
| `write_test` | `(path, content)` | Create a new test file |
| `run_tests` | `(suite?, before_patch=bool)` | Run target's test suite locally; supports two-call before/after to verify the regression test |
| `git_branch` | `(name)` | Create branch from main in the local checkout |
| `git_commit` | `(message, paths)` | Commit specific files |
| `git_push` | `(remote, branch)` | Push to a fork or branch the fixer owns |
| `gh_pr_create` | `(title, body, draft=True, labels=[...])` | Open draft PR via gh; draft hardcoded to True |
| `record_doc` | `(path, content)` | For doc-only: drop a fragility note under `target/docs/chaos-findings/` |
| `check_denylist` | `(path) -> bool` | Returns True if path is denied; called before every edit |
| `check_codeowners` | `(path) -> list[str]` | Returns CODEOWNERS list; refuses if security-team owned |

## 5. Decision tree

```
top.confidence < 0.5  → action=NONE, reason="diagnosis confidence below threshold"

top.fix_class == "working-as-intended"
    → action=DOC_ONLY
    → write target/docs/chaos-findings/<experiment_id>.md
    → optionally open doc-only PR

top.fix_class in {"missing-retry","missing-timeout","missing-circuit-breaker","missing-fallback"}
    → action=CODE_PATCH
    → add the missing primitive at top.affected_paths
    → add regression test that fails without and passes with

top.fix_class in {"auth-control-gap","secret-handling"}
    → action=CODE_PATCH
    → FLAG for human review (label: needs-security-review)
    → narrow patch; add regression test

top.fix_class == "image-policy"
    → action=CONFIG_CHANGE
    → patch admission policy manifests (Kyverno/Gatekeeper)
    → add policy unit test (e.g., conftest)

top.fix_class == "test-gap"
    → action=CODE_PATCH (but only the test)
    → ADD the missing test that should have caught the original issue
    → no production code changes

top.fix_class == "config-change"
    → action=CONFIG_CHANGE
    → adjust k8s manifest / ConfigMap / env var
    → no regression test required if it's pure config (but justify in reasoning)

top.fix_class == "code-patch" (catch-all)
    → action=CODE_PATCH
    → narrowest possible diff
    → add regression test
```

## 6. Patch discipline

- **Minimal diff.** One concern. No drive-by refactors. No reformatting other files.
- **No conversational comments** in code. The PR body explains; the code stands on its own.
- **No references to chaos in code.** "// retry added per chaos exp-abc123" is forbidden. The code change must be defensible without that context.
- **Existing style preserved.** If the file uses tabs, use tabs. If it uses snake_case, don't introduce camelCase.

## 7. Test discipline

- The regression test must **fail on a clean checkout** without the patch.
- It must **not depend on the chaos engine** running. (Mock the dep, or use a unit test boundary.)
- It must be **deterministic** — no time-based flakes, no real network calls.
- It must be **fast** — under 1s if reasonably possible.
- Its name should describe the bug it prevents, not the chaos that found it. `test_cart_serves_503_when_redis_unavailable` ✅, `test_chaos_exp_abc123` ❌.

## 8. PR template

```markdown
## Auto-proposal from chaos experiment <experiment_id>

**Confidence:** <confidence>
**Fix class:** <fix_class>
**Diagnosis:** <one-line summary from top hypothesis>

### What this PR changes
<file 1>: <brief>
<file 2>: <brief>

### Why
<diagnosis evidence: log snippets, trace ids, code:line references>

### Regression test
`<path/to/test_*.py>` — fails without this PR, passes with it.

### Reviewer checklist
- [ ] Is the fix correct, or is the diagnosis wrong?
- [ ] Is the regression test the right shape?
- [ ] Should we also update docs / runbooks?
- [ ] Are there other places in the codebase with the same pattern?

### Experiment record
<link to experiment artifacts in this repo's experiments/runs/ or external store>

---
Generated by `chaos` — **draft**, do not merge without review.
```

## 9. Implementation plan

### Milestone 6.0 — denylist + dry-run output (1 day)

- [ ] `check_denylist` (default: `.github/`, `infra/`, `secrets/`, `**/secrets.yaml`)
- [ ] `check_codeowners` parses target's CODEOWNERS if present
- [ ] `propose_fix()` for `action=NONE` and `action=DOC_ONLY` paths
- [ ] No PR yet — outputs the proposed diff/doc to `experiments/runs/<id>/proposed/`
- [ ] Acceptance: a `working-as-intended` diagnosis yields a real markdown file

### Milestone 6.1 — code-patch path with local edit (2 days)

- [ ] `read_target_code` (reuse tester sandbox), `edit_target_code`, `write_test`
- [ ] `run_tests` to validate the patch+test pair
- [ ] Acceptance: a missing-retry diagnosis on OTel demo cartservice produces a patch + test that passes locally

### Milestone 6.2 — branch + commit + push (1 day)

- [ ] `git_branch`, `git_commit`, `git_push` to a fixer-owned fork
- [ ] Acceptance: a real branch lands on a test fork

### Milestone 6.3 — gh pr create (1 day)

- [ ] `gh_pr_create` with `--draft` hardcoded, labels `chaos-fixer-proposal` + `confidence-{low,med,high}` + (optional) `needs-security-review`
- [ ] PR body assembled from template
- [ ] Acceptance: a real draft PR appears on the test fork; the URL is in `FixProposal.pr_url`

### Milestone 6.4 — config-change + doc-only flows (1–2 days)

- [ ] Path differentiation: when fix-class is config, patch only k8s manifests
- [ ] doc-only: writes markdown, may or may not open a PR (configurable)
- [ ] Acceptance: image-policy diagnosis produces a Kyverno policy patch PR

### Milestone 6.5 — pre-flight checks (1 day)

- [ ] Max-open-PR check (default 3); refuse if exceeded
- [ ] Stale branch cleanup (older than 14 days)
- [ ] Conflict detection (refuse if target file changed upstream since baseline)
- [ ] Acceptance: re-running the same experiment when a fixer PR is already open does NOT open a duplicate

## 10. Testing strategy

| Level | What's tested |
|---|---|
| Unit | Denylist enforcement |
| Unit | CODEOWNERS parsing |
| Unit | Decision-tree logic for each fix class |
| Unit | PR body template renders correctly |
| Integration | Against a local target checkout, full patch + test + run cycle |
| Integration | Against a test fork, real `gh pr create` (uses ephemeral repo) |

`is_draft=False` regression: the Pydantic contract test in `tests/test_contracts.py` already catches this. Keep it.

## 11. Failure modes

| Symptom | Cause | Handling |
|---|---|---|
| Patch doesn't compile | Diagnosis wrong / patch malformed | Reject, action=NONE, reasoning explains |
| Test passes without the patch | Bad regression test | Reject, write better test or action=NONE |
| Test fails *with* the patch | Patch doesn't fix | Reject |
| Diff includes denylisted path | Caller bug | Hard fail; surface to operator |
| Diff edits CODEOWNERS-bound file | Security boundary | Add `needs-security-review` label or refuse entirely (configurable) |
| Target repo has merge conflicts | Target moved on | Rebase or fail-loud — never force-push |
| gh CLI not authenticated | Setup issue | Hard fail with "run `gh auth login`" |

## 12. Budget profile

| Action | Tokens | $ | Wall-clock |
|---|---|---|---|
| action=NONE | ~5k | $0.05 | 30s |
| DOC_ONLY | 10–30k | $0.10–$0.30 | 1–3 min |
| CODE_PATCH (narrow) | 30–100k | $0.50–$1.50 | 3–10 min |
| CODE_PATCH (broad / multi-file) | 100–300k | $1.50–$5 | 10–30 min |

Soft cap: $2. Hard cap: $8.

## 13. Dependencies

- `gh` CLI authenticated
- Target's repo checkout writable (or a fork the fixer owns)
- Target's test framework runnable locally
- `git` ≥ 2.30

## 14. Open decisions

1. **Should the fixer ever open non-draft PRs?** No. Contract enforces draft. Don't relax this.
2. **Where does the fixer get write access to target repo?** Options: (a) push to a fork, open PR from fork; (b) push to a `chaos/<experiment_id>` branch in main repo. **Recommend (a)** to minimize blast radius. Repo owner can grant maintainer-edit on the fork's PR.
3. **Multiple top hypotheses with similar confidence — which one wins?** Top by confidence ties go to the simpler fix class (config > code). Recommend documenting alternates in reasoning.
4. **Self-reviewed fixes?** Out of scope. The fixer never reviews its own output; humans always do.
5. **Auto-rebase PR on conflict?** v2. v1 fails loud.

## 15. Acceptance criteria — "the fixer is done"

- All five action paths exercised at least once on a real target
- Pydantic contract enforces draft (already done)
- Path denylist + CODEOWNERS enforcement tested
- At least one PR opened during a real loop has been **merged by a human** after review
- Multi-confidence handling: alternate hypotheses documented in PR body when within 0.1 of top
- `scripts/integration-test.sh` opens and closes a PR on a test fork

## Folder layout

```
agents/fixer/
├── README.md             # this file
├── agent.py              # ClaudeFixerAgent
├── tools.py              # edit/write/git/gh wrappers (TBD)
├── pr_template.py        # PR body assembly (TBD)
├── decision.py           # fix-class -> action mapping (TBD)
├── prompts/
│   └── fix.md
├── scripts/              # dev scripts
└── tests/
    └── fixtures/         # diagnosis fixtures (TBD)
```
