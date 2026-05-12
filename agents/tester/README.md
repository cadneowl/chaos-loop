# tester agent — implementation plan

<img src="../../docs/cast/tester.png" alt="The Tester · the statistician" width="280" align="right" />

> *"Trust nothing. Sample everything. Especially 'looks fine to me'."*
> — [The Tester](../../docs/CAST.md#the-tester--the-statistician)

> **Alias:** the "Tester" agent from the original proposition.
> **Role:** establish a statistical baseline of healthy behavior, verify that the same probes still pass after chaos, and generate hypotheses by reading the target's source code.

## Current implementations

The hypothesize step has four implementations behind one Protocol — pick via `--profile` on `chaos run`:

| Implementation | What it does | Cost | File |
|---|---|---|---|
| `FixtureHypothesizer` | returns a predetermined list (tests + dry-run) | $0 | `agents/tester/hypothesizer.py` |
| `StaticHypothesizer` | runs pattern-match detectors over the target's code | **$0** | `agents/tester/hypothesizer.py` + `agents/tester/detectors/` |
| `HybridHypothesizer` | Static (always) + LLM (augmenting); falls back to Static on failure | $$ | `agents/tester/hypothesizer.py` |
| `ClaudeHypothesizer` | LiteLLM-backed; calls a model (Claude / Ollama / OpenAI) with MCP-style tools that read code | $$$ | `agents/tester/hypothesizer.py` |

Baseline and verify are pure Python (no LLM — they just run probes against Prometheus and compute `StatisticalSample` distributions). See `ClaudeTesterAgent.baseline()` and `verify()`.

### Static detectors (no LLM)

`agents/tester/detectors/` contains the deterministic pattern-match floor. Each detector emits `Issue` objects that get templated into `Hypothesis` instances with the right catalogue-mapped fault.

| Detector | Pattern | Maps to |
|---|---|---|
| `MissingTimeoutDetector` | http / subprocess calls without `timeout=` | `network.delay` |
| `MissingRetryDetector` | external-dep calls in a file with no retry primitive | `network.loss` |
| `MissingCircuitBreakerDetector` | external-dep call sites in a file with no circuit-breaker primitive (pybreaker / circuitbreaker / aiobreaker / hyx) | `network.partition` |
| `NoFallbackForCacheDetector` | cache GET-shaped calls (redis / valkey / memcache / aiocache) in a file that imports a cache and has no `try/except` | `pod.kill` |
| `SyncCallInAsyncDetector` | known-sync blocking calls (`time.sleep` / `requests.*` / sync subprocess / socket) inside an `async def` body, not offloaded via `to_thread` / `run_in_executor` | `network.delay` |
| `SingleReplicaDetector` | k8s Deployments with `replicas: 1` | `pod.kill` |
| `HardPodAffinityDetector` | `requiredDuringSchedulingIgnoredDuringExecution` | `pod.kill` |
| `HardcodedSecretDetector` | secret-suggestive names assigned to long string literals (skips env-loaded patterns + comments) | `secret.rotate` |

Add a detector: implement the `Detector` Protocol (one `find(code) -> list[Issue]` method), add a template entry to `_DETECTOR_CONFIG` in `hypothesizer.py`, register in `default_detectors()`.

## 1. Mission

The tester is the **first** and **last** agent in every loop iteration. Without it, there is no baseline to regress from and no verdict to act on.

It owns three modes:

| Mode | When invoked | Output |
|---|---|---|
| `baseline` | Before any fault | Statistical distributions for every probe (steady_state = True/False) |
| `verify` | After fault cleanup | Same probes, compared against baseline → steady or regressed |
| `hypothesize` | Optional pre-flight | Code-read hypotheses worth testing, mapped to faults in the catalogue |

## 2. Position in the loop

```
plan loaded ─► [tester.baseline] ─► [security.baseline] ─► safety gate
                                                            │
                                                            ▼
                                                  [chaos.execute]
                                                            │
                                                            ▼
              [tester.verify] ◄── [security.verify] ◄───────┘
                  │
                  ▼
       steady? ── yes ──► record + done
                  │
                  no
                  │
                  ▼
            [diagnostician]
```

The tester does **not** decide whether to inject. The orchestrator does that based on the tester's `steady_state` field.

## 3. Contract

Defined in `shared/contracts.py`. Don't change without bumping the schema version.

### Input — `TesterRequest`

```python
class TesterRequest(BaseModel):
    kind: Literal["baseline", "verify", "hypothesize"]
    experiment_id: ExperimentId
    target_app: str                    # short identifier, e.g. "otel-demo"
    target_repo: str | None = None     # git URL — required for hypothesize mode
    baseline_run_count: int = 5        # how many times to run each probe; 1..50
    probes: list[str] = []             # empty = use the default probe set for this target
```

### Output — `TesterReport`

```python
class TesterReport(BaseModel):
    request_kind: Literal["baseline", "verify", "hypothesize"]
    experiment_id: ExperimentId
    run_id: RunId                                 # auto-generated
    steady_state: bool                            # the single binary signal the orchestrator gates on
    samples: list[StatisticalSample]              # one per metric: mean/p50/p95/p99/stdev/raw
    failed_probes: list[str]                      # probe IDs that did not pass
    anomalies: list[str]                          # free-text descriptions of distribution drift
    generated_hypotheses: list[Hypothesis]        # populated only when kind="hypothesize"
    started_at: datetime
    finished_at: datetime | None
    notes: str                                    # caveats, context for next agent
```

### Inter-mode invariants

- `baseline.steady_state=False` → orchestrator aborts with `BASELINE_UNHEALTHY`. Never inject chaos on top of an already-broken system.
- `verify.steady_state=False` → orchestrator transitions to `diagnose`. The agent should populate `failed_probes` and `anomalies` with enough specificity that the diagnostician knows where to dig.
- `hypothesize.generated_hypotheses` non-empty is the success condition; `steady_state` is irrelevant in this mode.

## 4. Tool surface

The agent's Claude Agent SDK session is given exactly this toolset. No more, no less. The orchestrator owns the tool implementations and passes them in.

| Tool | Signature | Returns | Notes |
|---|---|---|---|
| `run_unit_tests` | `(target_repo: str, suite: str \| None = None) -> dict` | `{passed: int, failed: int, errors: list, duration_s: float, per_test: dict}` | Executes target's test runner (pytest/jest/go test) in a sandboxed checkout |
| `run_playwright` | `(target_url: str, suite_path: str) -> dict` | `{passed: int, failed: int, results: list, screenshots: list[str]}` | Runs Playwright spec files; screenshots only on failure |
| `query_prometheus` | `(query: str, range_seconds: int = 300, step_seconds: int = 15) -> list[dict]` | `[{timestamp, value, labels}]` | PromQL; auto-discovers the target's services |
| `query_loki` | `(logql: str, range_seconds: int = 300, limit: int = 1000) -> list[dict]` | `[{timestamp, line, labels}]` | LogQL; pass a service label selector |
| `read_target_code` | `(path: str, line_start: int \| None, line_end: int \| None) -> str` | File contents | Read-only; rejects paths outside the target's checkout root |
| `list_target_code` | `(glob: str) -> list[str]` | File list | Glob within target's checkout root |
| `grep_target_code` | `(pattern: str, glob: str \| None) -> list[dict]` | `[{path, line, text}]` | ripgrep within target's checkout |
| `record_sample` | `(metric: str, samples: list[float]) -> None` | — | Persists a sample list to the run; orchestrator computes the StatisticalSample |
| `lookup_baseline` | `(target_app: str, metric: str) -> StatisticalSample \| None` | Prior baseline distribution | Used in verify mode to compare |

## 5. Prompts

- `prompts/baseline.md` — establish a baseline; honest about uncertainty
- `prompts/verify.md` — compare to baseline using the v1 heuristic (deterministic thresholds)
- `prompts/hypothesize.md` — propose 1–10 hypotheses with file/line references

The prompts are deliberately strict about **steady-state being conservative**: a false-positive regression wastes one experiment; a false-negative steady-state corrupts every experiment that follows.

## 6. Implementation plan

### Milestone 2.0 — single-shot baseline (1–2 days)

- [ ] Implement `_tools.py` with `query_prometheus`, `query_loki`, `read_target_code`, `list_target_code`, `grep_target_code`, `record_sample`, `lookup_baseline`. Sandboxed FS roots.
- [ ] Implement `agent.py` `baseline()` using Claude Agent SDK against `prompts/baseline.md`
- [ ] One probe set hardcoded for OTel demo: frontend HTTP 200 rate, cart latency p95, checkout success rate
- [ ] Acceptance: `chaos run experiments/examples/01-redis-network-loss.yaml` produces a real `TesterReport.baseline` from the running cluster (no chaos injected yet)

### Milestone 2.1 — N-run statistical baseline (1 day)

- [ ] Drive the probe set N times back-to-back with quiet windows
- [ ] Aggregate samples into `StatisticalSample` (already in contracts)
- [ ] Acceptance: 5-run baseline shows non-zero stdev for noisy metrics

### Milestone 2.2 — unit test + Playwright probes (2 days)

- [ ] Implement `run_unit_tests` — clone target repo to a temp dir, run the target's test command, parse output
- [ ] Implement `run_playwright` — generate or load specs from the target's playwright config; capture screenshots on failure
- [ ] Acceptance: both probe types contribute to `failed_probes`/`samples`

### Milestone 2.3 — verify mode (1 day)

- [ ] Implement `verify()` flow: load baseline via `lookup_baseline`, re-run probes, apply v1 heuristic
- [ ] Acceptance: a probe with intentionally-bad numbers post-mock-chaos returns `steady_state=False`

### Milestone 2.4 — hypothesize mode (2–3 days)

- [ ] Implement `hypothesize()` using `prompts/hypothesize.md`
- [ ] Agent reads code, emits Pydantic-valid `Hypothesis` objects with file/line refs and a fault from `_meta.CATALOGUE`
- [ ] Acceptance: against OTel demo, agent generates at least 3 hypotheses, ≥1 with confidence > 0.7, all mapping to valid faults

## 7. Testing strategy

| Level | What's tested | Where |
|---|---|---|
| Unit | `StatisticalSample.from_samples` math | `shared/` (already done) |
| Unit | Tool sandbox boundaries (refuses paths outside target root) | `tests/test_tester_tools.py` (TBD) |
| Integration | `baseline()` against a recorded Prometheus/Loki fixture | `tests/test_tester_baseline.py` (TBD) |
| Integration | `verify()` heuristic — pass with same distribution, fail with shifted | `tests/test_tester_verify.py` (TBD) |
| E2E | Real kind cluster, OTel demo, both modes | `agents/tester/scripts/integration-test.sh` |

## 8. Failure modes

| Symptom | Likely cause | Handling |
|---|---|---|
| Tool returns empty | Service down or label mismatch | Mark probe as `failed`, do NOT silently treat as steady |
| Prometheus timeout | Cluster overloaded | Retry with backoff once; then fail-loud — never fake data |
| Hypothesis cites non-existent file | Hallucination | Reject the hypothesis at validation time (Pydantic + post-validator) |
| Steady-state when reality is regressed | Threshold too loose | Tighten via `notes`; future: per-target tuned thresholds |
| Steady-state-not-determined | Noisy baseline | Return `steady_state=False` with clear `anomalies`; orchestrator will skip the experiment |

## 9. Budget profile (rough)

| Mode | Typical tokens | $ at Opus 4.7 rates | Wall-clock |
|---|---|---|---|
| baseline (5 runs × 3 probes, no code reading) | 5–10k | $0.05–$0.15 | 30–90s |
| verify | 5–10k | $0.05–$0.15 | 30–90s |
| hypothesize (one-time per target) | 50–200k (reads code) | $0.50–$3.00 | 2–10 min |

Soft cap per invocation: $0.50 (baseline/verify), $3 (hypothesize). Hard cap: 2x soft.

## 10. Dependencies

- Prometheus, Loki running and reachable from the orchestrator host
- Target's repo checkout cached locally
- `pytest` / `npm` / `go` etc. for `run_unit_tests` — installed in a sandbox container if not on PATH
- Playwright with Chromium installed for `run_playwright`

## 11. Open decisions

1. **Where do probe definitions live?** Options: (a) per-target YAML in `target/`; (b) hardcoded in agent; (c) auto-discovered from target's existing CI config. **Recommend (a) for v1, migrate to (c) once we have multiple targets.**
2. **Sandbox for test execution?** Run target's tests in a Docker container vs. host. **Recommend container** to avoid polluting host Python env and to scope filesystem access.
3. **Comparison heuristic — v1 (deterministic thresholds) vs v2 (Mann-Whitney U)?** Start with v1 for explainability; move to statistical tests once we have enough runs to calibrate.
4. **How long is a "quiet window"?** Default 60s; needs tuning per target.

## 12. Acceptance criteria — "the tester is done"

- All three modes work end-to-end against OTel demo from `chaos run`
- ≥80% of regressions injected by `chaos/` are caught (recall)
- ≤10% false-positive rate on a healthy system (precision)
- One real hypothesis generated by `hypothesize` mode leads to a real chaos finding within milestone 7
- `scripts/integration-test.sh` passes in CI against a kind cluster

## Folder layout

```
agents/tester/
├── README.md             # this file
├── agent.py              # ClaudeTesterAgent implementing the Protocol
├── tools.py              # tool implementations (TBD)
├── probes/               # per-target probe definitions (TBD)
├── prompts/
│   ├── baseline.md
│   ├── verify.md
│   └── hypothesize.md
├── scripts/              # dev/diagnostic scripts (see scripts/README.md)
└── tests/                # agent-specific tests (TBD)
```
