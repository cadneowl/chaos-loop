# chaos agent — implementation plan

<img src="../../docs/cast/chaos_destroyer.png" alt="The Chaos Goblin · small, fast, gleeful" width="280" align="right" />

> *"replicas: 1? For ME? You shouldn't have."*
> — [The Chaos Goblin](../../docs/CAST.md#the-chaos-goblin--small-fast-gleeful)

> **Alias:** "Chaos Destroyer" from the proposition.
> **Role:** execute one or more `FaultSpec` entries by rendering Chaos Mesh CRDs (or invoking custom action handlers for security-flavored faults), apply them, observe lifecycle, clean up, and return a precise `ChaosTimeline`.

## 1. Mission

The chaos agent is the **only** component allowed to introduce failure into the target. Everything else observes; this one acts.

Design priorities, in order:
1. **Safety.** Refuse to apply anything outside the safety envelope.
2. **Accuracy.** Inject what was asked. Report the timeline truthfully.
3. **Cleanup.** Especially on error. Especially on abort.
4. **Determinism.** A given `FaultSpec` should produce the same CRD bytes every time.

The agent is **deliberately not very Claude-driven**. Most of its work is mechanical CRD rendering + kubernetes API calls. The LLM surface exists only for two situations:
- An unrecognized CRD status — ask the model "what does this mean, is it terminal?"
- A pre-flight rationale generation — annotate the CRD with a human-readable "why this experiment" string for kubectl viewers

## 2. Position in the loop

```
              orchestrator
                  │
                  │  plan (already safety-validated)
                  ▼
            chaos.execute  ─────► Chaos Mesh CRDs ─► target
                  │
                  │  ChaosTimeline (timestamps + lifecycle events)
                  ▼
              orchestrator
                  │
                  │  on abort / completion
                  ▼
            chaos.cleanup
```

## 3. Contract

### Input — `ExperimentPlan` (full plan, not just faults)

The agent needs the plan to honor `quiet_window_pre_seconds` / `quiet_window_post_seconds` and to read `safety.allow_multi_fault`.

### Output — `ChaosTimeline`

```python
class ChaosTimeline(BaseModel):
    experiment_id: ExperimentId
    events: list[TimelineEvent]   # ordered, monotonically increasing timestamps
    success: bool                 # all faults reached "cleaned-up"
    error: str | None             # populated when success=False
```

```python
class TimelineEvent(BaseModel):
    timestamp: datetime
    fault_name: str
    event: Literal["scheduled", "started", "verified-active", "stopped", "cleaned-up", "error"]
    detail: str
```

The timeline is the **source of truth** for the diagnostician. Bad timestamps = bad attribution.

## 4. Tool surface

The agent's tools split into two layers:

### Layer A — kubernetes I/O (synchronous, idempotent)

| Tool | Signature | Purpose |
|---|---|---|
| `render_crd` | `(fault: FaultSpec) -> dict` | Look up renderer in `faults/_meta.py`, return CRD body |
| `apply_crd` | `(body: dict) -> str` | Apply the CRD; returns resource name |
| `get_crd_status` | `(kind: str, name: str, namespace: str) -> dict` | Poll status.phase, conditions |
| `delete_crd` | `(kind: str, name: str, namespace: str) -> None` | Idempotent delete |
| `list_crds` | `(experiment_id: str) -> list[dict]` | All CRDs labeled with this experiment_id |
| `wait_for_status` | `(kind, name, ns, phase: str, timeout_s: int) -> dict` | Poll until phase reached or timeout |
| `sleep_until` | `(ts: datetime) -> None` | Wait for quiet windows |

### Layer B — security-flavored fault helpers (where chaos_mesh_kind=None)

| Tool | Used by | Purpose |
|---|---|---|
| `patch_secret` | `secret.rotate` | Patch a k8s Secret in-place |
| `swap_deployment_image` | `image.swap_vuln`, `image.swap_unsigned` | kubectl set image, expect admission rejection |
| `delete_netpol` | `netpol.regress` | Delete and verify dropped |
| `run_egress_probe` | `egress.exfil_sim` | Exec a curl into a target pod toward the controlled sink |
| `touch_rootfs` | `runtime.tamper` | Exec write into a container's rootfs to trip runtime sensors |

These helpers do exactly one thing each. They never improvise.

## 5. Fault catalogue (already declared in `faults/_meta.py`)

22 entries. Categories:
- **Classical** (Chaos Mesh native): `pod.kill`, `pod.failure`, `network.loss/delay/partition`, `io.latency`, `stress.cpu/memory`, `dns.error`, `http.abort`, `time.skew`
- **Security-flavored**: `cert.revoke/expire`, `tls.downgrade`, `auth.outage/latency`, `secret.rotate`, `image.swap_vuln/unsigned`, `iam.degrade`, `netpol.regress`, `egress.exfil_sim`, `runtime.tamper`

See [../../docs/SECURITY_CHAOS.md](../../docs/SECURITY_CHAOS.md) for the security-flavored faults' hypotheses and rationale.

## 6. Attribution discipline (v1 invariant)

**One fault per experiment.** Multi-fault is v2 and requires:
- `safety.allow_multi_fault=True`
- Each fault's timeline labeled distinctly
- Diagnostician understands non-overlapping windows
- ChaosTimeline events tagged with the fault.name they belong to

Quiet windows (`pre`, `post`) are sacred. The tester relies on them for clean comparisons.

## 7. Implementation plan

### Milestone 3.0 — minimum viable injection (2 days)

- [ ] Implement `render_crd` for: `pod.kill`, `network.loss`, `network.delay`, `stress.cpu`. Use the Chaos Mesh API reference (v2.7 API).
- [ ] Implement `apply_crd`, `get_crd_status`, `delete_crd`, `wait_for_status` via `kubernetes` Python client.
- [ ] Implement `execute()` happy path: render → apply → wait running → sleep duration → delete → wait cleaned. Emit timeline events.
- [ ] Implement `cleanup()` — delete every CRD labeled with the experiment_id, ignore not-found.
- [ ] Acceptance: `chaos run examples/01-redis-network-loss.yaml` (no `--dry-run`) injects a real NetworkChaos, OTel demo cart shows real latency spike in Grafana, CRD is gone 60s after duration ends.

### Milestone 3.1 — full classical catalogue (2 days)

- [ ] Renderers for remaining classical: `pod.failure`, `network.partition`, `io.latency`, `stress.memory`, `dns.error`, `http.abort`, `time.skew`
- [ ] Per-renderer tests with golden CRD YAML in `agents/chaos/tests/golden/`
- [ ] Acceptance: every classical fault has a passing render test and at least one applies successfully against the kind cluster

### Milestone 3.2 — security-flavored faults, the easy ones (3 days)

- [ ] `cert.revoke` — NetworkChaos blocking OCSP/CRL domains
- [ ] `cert.expire` — TimeChaos skew past NotAfter
- [ ] `auth.outage` / `auth.latency` — NetworkChaos against IdP path
- [ ] `secret.rotate` — `patch_secret` helper
- [ ] Acceptance: each runs against OTel demo (or a fixture), CRD/action visible in events

### Milestone 3.3 — security-flavored faults, the gated ones (3–5 days)

- [ ] `image.swap_vuln`, `image.swap_unsigned` — require Kyverno or Gatekeeper installed; verifies admission rejection
- [ ] `netpol.regress` — delete-and-verify-still-denied
- [ ] `iam.degrade` — NetworkChaos partial loss to STS path (only on clusters with workload identity)
- [ ] `egress.exfil_sim`, `runtime.tamper` — gated behind `requires_approval=True` + cluster annotation `chaos.kosta.dev/security-allowed=true`
- [ ] Acceptance: each fault either succeeds (verified positive result) or fails-loud with a clear "this cluster doesn't support X" message — never silently no-ops

### Milestone 3.4 — robustness (1–2 days)

- [ ] Timeline guarantees: every CRD apply followed by a verify-active poll within 30s, or we mark the fault as `error` and abort
- [ ] Idempotent re-runs: re-applying the same experiment cleans prior CRDs first
- [ ] Orphan cleanup: `agents/chaos/scripts/force-cleanup.sh` finds and deletes any chaos-* CRDs in a namespace

## 8. Testing strategy

| Level | What's tested | Where |
|---|---|---|
| Unit | Each renderer produces deterministic CRD bytes for a given FaultSpec | `tests/test_chaos_render.py` |
| Unit | Catalogue lookup rejects unknown names | `tests/test_contracts.py` (already covers) |
| Integration | Full execute/cleanup roundtrip against kind | `agents/chaos/scripts/integration-test.sh` |
| E2E | Real CRD applied, target observes effect, CRD cleaned | as part of `chaos run` |

### Golden file pattern

Each renderer has a corresponding `tests/golden/<fault_name>.yaml`. The test renders from a fixed `FaultSpec` and diffs against the golden. A change requires regenerating the golden — explicit, never automatic.

## 9. Failure modes

| Symptom | Likely cause | Handling |
|---|---|---|
| CRD applies, never reaches `Running` | Chaos Mesh controller down or no targets matched | Wait 30s, mark `error`, abort experiment, run cleanup |
| CRD reaches `Running` but target shows no effect | Selector mismatch or RBAC | Treat as `started` (timeline truthful); diagnostician will surface it |
| Cleanup hangs | Finalizer stuck on CRD | Force-finalize via patch; surface as warning in timeline |
| Orphan CRDs from prior crash | Crashed before cleanup | `list_crds(experiment_id)` on next run; clean before applying new |
| Image-swap admission unexpectedly succeeds | Policy missing or misconfigured | This IS the finding — return success with timeline event "admission accepted (unexpected)" |
| Multi-fault requested without flag | Caller bug | Reject with explicit error; never silently single-fault |

## 10. Budget profile

Most operations are kubernetes API calls, not LLM calls.

| Operation | LLM tokens | Wall-clock |
|---|---|---|
| `execute(single classical fault)` | 0 (no LLM) | `duration_seconds + 60s` (status polls) |
| `execute(security fault)` | 0–5k (only if admission decision is ambiguous) | `duration_seconds + 30s` |
| `cleanup` | 0 | 5–30s |

Soft cap per experiment: $0.05. Hard cap: $0.20.

## 11. Dependencies

- `kubernetes` Python client
- Chaos Mesh ≥ 2.6 installed in cluster
- Kyverno or Gatekeeper installed (only for `image.*` faults)
- A `cosign` public key configured on the cluster's image policy controller (only for `image.swap_unsigned`)

## 12. Open decisions

1. **Render via raw Python dicts vs Chaos Mesh Go-generated CRD models?** Python dicts are easier; Go models give type safety. **Recommend dicts for v1, switch later if pain.**
2. **Polling vs watching CRDs?** Polling is simpler; watching is more responsive. **Recommend polling (5s interval) for v1.**
3. **Where do verification probes for security faults live?** E.g., for `image.swap_vuln`, how do we *verify* admission denied? Options: in the chaos agent (it owns the action), in the security agent (it owns DAST), or split. **Recommend chaos agent verifies admission outcomes (synchronous), security agent verifies steady-state security properties post-fault (asynchronous).**
4. **Multi-tenant cluster support?** Out of scope for v1.

## 13. Acceptance criteria — "the chaos agent is done"

- Every fault in `_meta.CATALOGUE` has: a renderer (or action handler), a golden test, integration test passing on kind
- `execute()` never leaks CRDs even on hard kill (cleanup-on-error verified by chaos-monkeying the agent itself)
- `ChaosTimeline` events match what `kubectl get podchaos -o yaml | grep transition` shows
- A complete experiment runs end-to-end against OTel demo with the chaos agent contributing real lifecycle data

## Folder layout

```
agents/chaos/
├── README.md             # this file
├── agent.py              # ClaudeChaosAgent implementing the Protocol
├── chaos_mesh.py         # k8s client helpers (TBD)
├── faults/
│   ├── _meta.py          # catalogue (real)
│   ├── pod.py            # renderers (TBD)
│   ├── network.py
│   ├── io.py
│   ├── stress.py
│   ├── dns.py
│   ├── http.py
│   ├── time.py
│   ├── cert.py           # security-flavored renderers
│   ├── auth.py
│   ├── secret.py
│   ├── image.py
│   ├── netpol.py
│   ├── iam.py
│   ├── egress.py
│   └── runtime.py
├── prompts/
│   └── execute.md
├── scripts/              # dev scripts
└── tests/                # render goldens + integration (TBD)
```
