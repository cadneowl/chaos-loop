# diagnostician agent — implementation plan

<img src="../../docs/cast/diagnostician.png" alt="The Diagnostician · the detective" width="280" align="right" />

> *"It was the missing retry, in the cart service, with the 503 at 14:02:18."*
> — [The Diagnostician](../../docs/CAST.md#the-diagnostician--the-detective)

> **Aliases:** "Debugger" (from the proposition), "diagnostician" (in the SCE literature), "RCA agent."
> **Role:** when post-chaos verification fails, take the failed reports + chaos timeline + observability data + target source code, and produce a ranked list of root-cause hypotheses with evidence.

## Current implementations

Four variants behind one `Diagnoser` Protocol (`agents/diagnostician/diagnoser.py`):

| Implementation | How it works | Cost |
|---|---|---|
| `FixtureDiagnoser` | predetermined hypotheses (or async callback); tests + dry-run | $0 |
| `StaticDiagnoser` | maps chaos `FaultCategory` to candidate `(fix_class, confidence)` entries via a lookup table; boosts confidence based on symptom keywords in failed reports | **$0** |
| `HybridDiagnoser` | Static (always) + LLM (augmenting); merges on `(fix_class, affected_paths)`, higher confidence wins; falls back to Static on LLM failure | $$ |
| `ClaudeDiagnoser` | LiteLLM-backed; the model gets MCP-style tools for Loki / Prometheus / code reading bounded to the chaos window, returns ranked hypotheses | $$$ |

### What Static can and can't do

It can route a `network.loss` fault to `missing-retry` / `missing-timeout` / `missing-fallback` candidates and rank them by confidence based on symptom keywords (`"p95 latency"` boosts `missing-timeout`, `"5xx"` boosts `missing-retry`, etc.). It can't do real cross-evidence reasoning — that's LLM territory. The point of Hybrid is to give the loop a $0 floor while letting the LLM contribute when it's available.

## 1. Mission

The diagnostician is the **cognitive heart** of the loop. Tester and security tell us *what* broke; chaos tells us *what we did*; the diagnostician tells us *why* — and only the diagnostician.

Hard rules:
- **Hypotheses, never assertions.** Output is ranked candidates with confidence.
- **Cited evidence.** Every claim points at a log line, trace span, metric, or source line.
- **`working-as-intended` is a real answer.** Some chaos-revealed fragilities are by design; the right output is a documented note, not a fix.

## 2. Position in the loop

```
[tester.verify] OR [security.verify] reports steady_state=False
                                  │
                                  ▼
                          [diagnostician]
                              │
                              │  reads:
                              │   - failed report(s)
                              │   - chaos timeline (window!)
                              │   - logs (Loki)
                              │   - metrics (Prom)
                              │   - traces (Tempo)
                              │   - target source code
                              │   - prior similar records (similarity search over store)
                              │
                              ▼
                       DiagnosisReport
                       (1–5 ranked hypotheses)
                              │
                              ▼
                          [fixer] OR [doc-only]
```

## 3. Contract

### Input — `DiagnosisRequest`

```python
class DiagnosisRequest(BaseModel):
    experiment_id: ExperimentId
    failed_tester_report: TesterReport | None
    failed_security_report: SecurityReport | None
    chaos_timeline: ChaosTimeline           # source of truth for time windows
    target_repo: str | None
```

Pydantic validator: at least one of the failed reports must be present.

### Output — `DiagnosisReport`

```python
class DiagnosisReport(BaseModel):
    experiment_id: ExperimentId
    run_id: RunId
    hypotheses: list[RootCauseHypothesis]   # 1+, ordered by confidence desc
    notes: str
    started_at: datetime
    finished_at: datetime | None
```

```python
class RootCauseHypothesis(BaseModel):
    summary: str
    confidence: float                        # 0..1
    evidence: list[str]                      # each item: where to look (log/trace/code:line)
    suggested_fix_class: Literal[
        "code-patch", "config-change",
        "missing-retry", "missing-timeout", "missing-circuit-breaker", "missing-fallback",
        "auth-control-gap", "secret-handling", "image-policy",
        "test-gap",
        "working-as-intended",
    ]
    affected_paths: list[str]                # files in target_repo where the fix would land
```

## 4. Tool surface

The diagnostician has the largest tool surface because it has the broadest information access. But every tool is **read-only**. It cannot mutate anything.

| Tool | Signature | Purpose |
|---|---|---|
| `query_loki` | `(logql, start_ts, end_ts, limit=2000)` | Logs in the chaos window |
| `query_prometheus_range` | `(promql, start_ts, end_ts, step_s=15)` | Time series |
| `query_prometheus_instant` | `(promql, ts)` | Point-in-time |
| `list_traces` | `(service, start_ts, end_ts, status_filter)` | Trace headers |
| `get_trace` | `(trace_id)` | Full trace tree |
| `tempo_search` | `(query, start_ts, end_ts)` | TraceQL search |
| `read_target_code` | `(path, line_start?, line_end?)` | Source read |
| `list_target_code` | `(glob)` | File listing |
| `grep_target_code` | `(pattern, glob?)` | rg-equivalent |
| `prior_records` | `(target_app, similar_to: DiagnosisRequest)` | Similar past experiments |
| `chaos_fault_doc` | `(fault_name)` | What is this fault, what would it test |

## 5. Correlation discipline

A log line is only **evidence** if all three hold:
1. Timestamp in `[chaos_timeline.start - 30s, chaos_timeline.end + 60s]`.
2. References the affected service (label, span name, message text).
3. Specific enough that you can quote it.

Hypotheses with weak correlation get confidence < 0.5.

### Prior-art lookup

`prior_records` searches the SQLite store for past `ExperimentRecord`s with the same `target_app` + similar failure signature (failed_probe names, anomaly text). If we've seen this exact regression before, the prior diagnosis is included as a candidate (the diagnostician should consider but not blindly trust it).

## 6. Decision tree for `suggested_fix_class`

```
Did chaos disable a dependency the service has a hard requirement on?
├── yes, and code shows no retry/timeout/breaker → missing-{retry,timeout,circuit-breaker}
├── yes, and code shows a fallback path that hung → missing-fallback
├── yes, and the dep is documented as required → working-as-intended
└── no
    │
    ├── A test class exists that should have caught this but didn't → test-gap
    │
    ├── Failure is in a security boundary (auth, secret, image policy) →
    │      auth-control-gap | secret-handling | image-policy
    │
    ├── Fix is in a k8s manifest or env var → config-change
    │
    └── Fix is in application code → code-patch
```

## 7. Implementation plan

### Milestone 5.0 — read-only tool wrappers (1–2 days)

- [ ] `query_loki`, `query_prometheus_range`, `query_prometheus_instant`, `list_traces`, `get_trace`, `tempo_search`
- [ ] `read_target_code`, `list_target_code`, `grep_target_code` reusing tester sandbox
- [ ] `chaos_fault_doc` reads `agents/chaos/faults/_meta.py`
- [ ] Acceptance: each tool unit-tested against a fixture; running against kind/Loki/Tempo returns sane data

### Milestone 5.1 — single-hypothesis diagnose (2 days)

- [ ] Implement `diagnose()` with `prompts/diagnose.md`
- [ ] Constraint: produce exactly 1 hypothesis
- [ ] Acceptance: against a mocked failed report + real chaos timeline + real Loki data for OTel demo, produces a hypothesis with non-zero evidence

### Milestone 5.2 — ranked hypotheses + working-as-intended (2 days)

- [ ] Allow 1–5 hypotheses
- [ ] Prompt explicitly considers `working-as-intended`
- [ ] Acceptance: when given a regression caused by a documented hard dep (e.g., cart → valkey), at least one hypothesis is `working-as-intended` with high confidence

### Milestone 5.3 — prior-record similarity (2 days)

- [ ] `prior_records` simple similarity: same target_app, overlap in failed_probes, overlap in fault category
- [ ] Diagnostician prompt updated to consider prior diagnoses
- [ ] Acceptance: re-running the same experiment recognizes prior diagnosis and either reinforces (higher confidence) or notes the new info

### Milestone 5.4 — security path (1–2 days)

- [ ] When `failed_security_report` is set, prompt branch focuses on security fix classes (`auth-control-gap`, `secret-handling`, `image-policy`)
- [ ] Tools: ZAP report links, SBOM diff, kubescape findings as additional inputs
- [ ] Acceptance: a `cert.revoke` chaos that leaks the cert error to user-facing logs surfaces a `secret-handling` or `auth-control-gap` hypothesis

## 8. Testing strategy

| Level | What's tested |
|---|---|
| Unit | Tool wrappers with recorded fixtures |
| Unit | Schema validation (DiagnosisReport requires ≥1 hypothesis — already in contracts) |
| Integration | Fed a synthetic failed report + real Loki/Tempo from a controlled chaos run, produces sensible hypothesis |
| Integration | Faces a deliberately ambiguous case (multiple plausible causes) and outputs multiple hypotheses |
| E2E | Full loop where chaos induces a known fragility — diagnosis names the right file/line |

### Replay testing

The diagnostician's behavior is sensitive to prompt + tools. Build a fixture library under `agents/diagnostician/tests/fixtures/` of past failed runs, and `agents/diagnostician/scripts/replay.sh` re-runs the agent against them. CI compares output against a snapshot — drift requires explicit approval.

## 9. Failure modes

| Symptom | Likely cause | Handling |
|---|---|---|
| Output cites a file that doesn't exist | Hallucination | Post-validator rejects; lower confidence |
| All hypotheses confidence < 0.3 | Weak evidence | Surface honestly; fixer will skip |
| Diagnoses chaos itself as the cause | Insufficient understanding of fault catalogue | Fix the prompt; consider working-as-intended |
| Token explosion | Too-broad log queries | Hard token cap; force the agent to narrow window |
| Confident wrong answer | Plausible-sounding pattern match | This is why we never auto-merge — humans review |

## 10. Budget profile

| Mode | Tokens | $ | Wall-clock |
|---|---|---|---|
| diagnose (small regression, focused logs) | 30–80k | $0.30–$1.00 | 1–3 min |
| diagnose (broad regression, many traces) | 100–400k | $1–$5 | 3–10 min |
| diagnose (security path, full SBOM diff) | 80–250k | $0.80–$3.00 | 2–8 min |

Soft cap: $2.00. Hard cap: $8.00.

## 11. Dependencies

- Loki, Prometheus, Tempo reachable from orchestrator
- Target repo checkout (reuse tester's sandbox)
- SQLite store for prior records
- Agent SDK (Claude)

## 12. Open decisions

1. **Tempo vs Jaeger?** Tempo is in our default stack; Jaeger is more common in the wild. **Recommend Tempo for v1**, adapter pattern keeps options open.
2. **Should the diagnostician have access to the fixer's denylist?** Yes — there's no point producing a fix-class hypothesis that the fixer will refuse to act on. Pass denylist as context.
3. **Prior-records similarity — manual vs embeddings?** Manual keyword overlap is fine for v1. Embeddings are M6+.
4. **What about cross-experiment patterns?** "We've seen this 5 times" deserves higher confidence. v2.

## 13. Acceptance criteria — "the diagnostician is done"

- All fix classes appear in real output at least once
- `working-as-intended` correctly fires for known hard dependencies
- Re-running the same experiment yields consistent diagnosis (deterministic enough)
- Citations are always real (file exists, log line exists, trace ID exists)
- One end-to-end loop produces a diagnosis that, after human review, was correct

## Folder layout

```
agents/diagnostician/
├── README.md             # this file
├── agent.py              # ClaudeDiagnosticianAgent
├── tools.py              # tool implementations (TBD)
├── similarity.py         # prior_records logic (TBD)
├── prompts/
│   └── diagnose.md
├── scripts/              # dev scripts
└── tests/
    └── fixtures/         # replay corpus (TBD)
```
