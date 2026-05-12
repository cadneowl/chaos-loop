# security agent — implementation plan

<img src="../../docs/cast/security.png" alt="The Security Bouncer" width="280" align="right" />

> *"Your image is unsigned, your deps are stale, and we're not friends."*
> — [The Security Bouncer](../../docs/CAST.md#the-security-bouncer--your-names-not-on-the-sbom)

> **Alias:** the "Security" agent introduced in the follow-up to integrate DAST + supply chain analysis into the chaos lifecycle.
> **Role:** establish a security baseline of the target system, re-scan after chaos, detect runtime drift in the SBOM, and generate **security hypotheses** by reading SBOMs + auth code + cluster policies.

## 1. Mission

Security findings flow through the **same closed loop** as functional regressions. A regression detected by a CVE scanner, a DAST passive scan, or a posture check is treated structurally identically to a failed Playwright test.

The agent runs three modes:

| Mode | When | Output |
|---|---|---|
| `baseline` | Before any fault | SecurityReport with current findings + SBOM digest |
| `verify` | After chaos cleanup | SecurityReport; compares SBOM digest, re-runs ZAP + cosign |
| `drift` | Periodically during chaos | Cheap subset: SBOM digest only, detect runtime image swap |
| `hypothesize` | Optional pre-flight | SecurityHypotheses to drive future chaos plans |

This is the only agent (besides tester) that runs in **both** the pre-chaos and post-chaos windows.

## 2. Position in the loop

```
plan ─► tester.baseline ─► security.baseline ─► gate?
                                  │
                                  ▼
                          chaos.execute
                                  │
            ┌─── security.drift  (cheap, async during fault)
            │     │
            │     ▼
            └─── security.verify ◄── tester.verify
                                  │
                                  ▼
                            diagnose if any finding HIGH/CRITICAL
```

## 3. Contract

### Input — `SecurityRequest`

```python
class SecurityRequest(BaseModel):
    kind: Literal["baseline", "verify", "drift", "hypothesize"]
    experiment_id: ExperimentId
    target_app: str
    target_repo: str | None = None
    target_images: list[str]       # explicit list to scan; auto-discovered if empty
    target_endpoints: list[str]    # for ZAP; HTTP URLs
    enable_active_dast: bool = False  # gated, off by default
```

### Output — `SecurityReport`

```python
class SecurityReport(BaseModel):
    request_kind: Literal["baseline", "verify", "drift", "hypothesize"]
    experiment_id: ExperimentId
    run_id: RunId
    findings: list[SecurityFinding]                # CVEs, ZAP alerts, kubescape issues
    generated_hypotheses: list[SecurityHypothesis] # populated in hypothesize mode
    sbom_digest: str | None                        # sha256 of canonicalized SBOM
    sbom_drift_from_baseline: bool                 # set by verify/drift modes
    started_at: datetime
    finished_at: datetime | None

    @property
    def has_critical_or_high(self) -> bool: ...
```

`SecurityReport.has_critical_or_high == True` makes the orchestrator treat the experiment as regressed — even if the tester says steady-state.

## 4. Tool surface

Most of the agent's heavy lifting is shelling out to scanner CLIs. Each scanner lives in `scanners/<name>.py` with a single `run(...) -> list[SecurityFinding]` function (plus SBOM helpers that also return digests).

| Tool | Wraps | Modes used in | Time per run |
|---|---|---|---|
| `scan_sbom` | Syft | baseline, verify, drift | 10–30s per image |
| `scan_cve` | Grype | baseline, verify | 5–15s |
| `scan_image` | Trivy | baseline | 30–90s per image |
| `scan_dast_passive` | ZAP baseline-scan | baseline, verify | 1–3 min per endpoint |
| `scan_dast_active` | ZAP full-scan | opt-in only | 10–30 min per endpoint |
| `scan_secrets` | gitleaks | baseline | 5–30s per repo |
| `verify_signatures` | cosign | baseline, verify, drift | 1–3s per image |
| `scan_posture` | kubescape | baseline, verify | 30–90s per namespace |
| `discover_images` | kubectl | baseline | 1–3s |
| `read_target_code` | (sandboxed FS) | hypothesize | — |

## 5. Scanner-by-scanner integration steps

### Syft (`scanners/sbom.py`)

```bash
syft <image-ref> -o spdx-json | jq . > sbom-<image>.json
sha256sum sbom-<image>.json  # this is the digest
```

- Return value: `(findings, sbom_digest)`. Findings list is empty in baseline — Syft just emits the bill of materials. The CVE scanner converts BOM entries to findings.
- SBOM stored to `experiments/runs/<experiment_id>/sboms/<image>.spdx.json` for diffing.
- Drift detection: compare digest with baseline. If different and we didn't deliberately swap images, that's a finding.

### Grype (`scanners/sca.py`)

```bash
grype sbom:<path> -o json | jq .matches
```

- Each `matches[]` element → one `SecurityFinding` with severity mapped from Grype severity.
- CVE id → `finding.cve`.
- Suppression list: `config/grype-ignore.yaml` for known-and-accepted CVEs.

### Trivy (`scanners/image.py`)

```bash
trivy image --format json --severity CRITICAL,HIGH,MEDIUM <image>
```

- Combines vulns + misconfigs + secrets in one call. Pull out `Results[].Vulnerabilities` and `Results[].Misconfigurations`.

### OWASP ZAP (`scanners/dast.py`)

```bash
# Passive (default in baseline)
docker run --rm -t -v "$PWD/zap-reports:/zap/wrk:rw" owasp/zap2docker-stable \
  zap-baseline.py -t "$ENDPOINT" -J zap-baseline.json

# Active (opt-in)
docker run ... zap-full-scan.py -t "$ENDPOINT" -J zap-full.json
```

- Parse `site[].alerts[]` → SecurityFinding. Map riskdesc to FindingSeverity.
- Active scan is **gated** by `request.enable_active_dast`. The orchestrator's safety layer also requires `requires_approval=true` for any plan that enables it.

### gitleaks (`scanners/secrets.py`)

```bash
gitleaks detect --source <repo-path> --report-format json --report-path -
```

- Each leak → SecurityFinding with severity HIGH and `location = "file:line"`.
- Also run against `experiments/runs/<experiment_id>/logs/` after chaos to detect leaked secrets in error paths.

### cosign (`scanners/sign.py`)

```bash
cosign verify --key <pubkey> <image>            # keyed
cosign verify --certificate-identity ... <image> # keyless (Sigstore)
```

- Unsigned image → CRITICAL finding (image policy gap).
- Used by chaos agent's `image.swap_unsigned` fault for symmetric verification.

### kubescape (`scanners/posture.py`)

```bash
kubescape scan framework nsa --namespace <ns> --format json
```

- Map control failures to SecurityFinding with severity from kubescape's risk score.

## 6. Hypothesis generation (`hypothesize` mode)

The agent reads three sources to propose `SecurityHypothesis` entries:

1. **Baseline SBOM + Grype findings** → "what if CVE-affected service X is exercised under chaos? Does its degraded-mode path expose the vuln?"
2. **Target's auth/secrets code paths** (via `read_target_code` + `grep_target_code`) → fault candidates from `auth.*`, `secret.*`, `cert.*` categories.
3. **Cluster's NetworkPolicies + RBAC** → `netpol.regress`, `iam.degrade`.
4. **Image signatures** → `image.swap_unsigned` if any image has no signature.

Prompt: `prompts/hypothesize.md` (already written, uses MUST / MUST NOT phrasing).

## 7. Implementation plan

### Milestone 4.0 — SBOM + CVE baseline (1–2 days)

- [ ] `discover_images(namespace) -> list[str]` via kubectl
- [ ] `scan_sbom` for each image, store SBOM, compute digest
- [ ] `scan_cve` for each SBOM, map matches to SecurityFinding
- [ ] Wire into `SecurityAgent.baseline`
- [ ] Acceptance: real SecurityReport with ≥1 finding against OTel demo

### Milestone 4.1 — image + secrets + signatures (1 day)

- [ ] `scan_image` (Trivy), `scan_secrets` (gitleaks against `target_repo`), `verify_signatures` (cosign)
- [ ] Suppression file format documented
- [ ] Acceptance: each scanner contributes to findings, suppressions respected

### Milestone 4.2 — DAST baseline (1 day)

- [ ] Wrap ZAP baseline-scan in `scan_dast_passive`
- [ ] Save HTML report to `experiments/runs/<id>/zap/`
- [ ] Acceptance: ZAP scan of OTel demo frontend completes in <5 min, produces parsed findings

### Milestone 4.3 — posture (1 day)

- [ ] Wrap kubescape NSA framework
- [ ] Acceptance: kubescape findings flow through; failed controls become SecurityFinding

### Milestone 4.4 — verify mode + drift detection (1 day)

- [ ] In verify: re-run SBOM + DAST + cosign. Compare SBOM digest.
- [ ] In drift: SBOM digest only (cheap)
- [ ] Acceptance: chaos that swaps an image (e.g., `image.swap_vuln`) is detected by drift

### Milestone 4.5 — hypothesis generation (2–3 days)

- [ ] Implement `hypothesize()` with the prompt
- [ ] Tool surface: SBOM + Grype findings + code reading
- [ ] Acceptance: produces ≥3 hypotheses against OTel demo, all mapping to faults in catalogue

## 8. Testing strategy

| Level | What's tested |
|---|---|
| Unit | Each scanner's output parser against recorded fixtures in `agents/security/tests/fixtures/` |
| Unit | Suppression file logic |
| Integration | `baseline` against a kind cluster with OTel demo |
| Integration | `verify` after a known image swap detects drift |
| E2E | Full loop with `image.swap_vuln` chaos surfaces a SecurityReport regression that lands in diagnostician |

## 9. Failure modes

| Symptom | Cause | Handling |
|---|---|---|
| Scanner binary not on PATH | Install missed | Hard-fail with explicit "run scripts/install.sh" message |
| ZAP scan times out | Endpoint unreachable | Mark scanner result as `error`, do NOT fake findings |
| SBOM digest changes for no reason | Non-deterministic packaging | Canonicalize the SBOM (sort keys, strip timestamps) before hashing |
| Hypothesis cites non-existent CVE | Stale Grype DB | Refresh DB; reject hypotheses without DB hits |
| False-positive flood | Outdated suppression list | Snooze finding in `config/grype-ignore.yaml` with expiration date |

## 10. Budget profile

| Mode | LLM tokens | Wall-clock | $ |
|---|---|---|---|
| baseline | 5–20k (just orchestration) | 3–8 min (CLI-bound) | $0.10–$0.40 |
| verify | 5–20k | 2–5 min | $0.10–$0.30 |
| drift | 0 (no LLM) | 30s | $0 |
| hypothesize | 50–200k (reads code + CVEs) | 5–15 min | $1–$5 |

## 11. Dependencies

- Syft, Grype, Trivy, gitleaks, cosign, kubescape CLIs on PATH (see `infra/security-tools/install.sh`)
- Docker daemon for ZAP container
- Network access from orchestrator host to target's HTTP endpoints (for ZAP)
- Target's images pullable locally (for Syft/Trivy)
- Updated Grype CVE database (`grype db update`)

## 12. Open decisions

1. **SBOM format — SPDX vs CycloneDX?** Both are supported. **Recommend SPDX-JSON** (Syft's default, broad tool support).
2. **Where do suppressions live?** Options: per-target in `target/`, global in `config/`. **Recommend per-target** — different apps have different acceptable risks.
3. **Should we mirror the target's images locally for offline scanning?** Speed up scans + isolate from registry rate limits. **Recommend yes** when target is bundled.
4. **DAST against staging vs prod?** Always staging. The orchestrator's cluster denylist prevents prod even if asked.
5. **Active DAST as part of any auto-flow?** No — always opt-in per experiment.

## 13. Acceptance criteria — "the security agent is done"

- All 4 modes work against OTel demo
- Every scanner is wrapped and has parser tests
- SBOM drift correctly fires on `image.swap_*` faults
- At least one security hypothesis from `hypothesize` leads to a real chaos finding by milestone 7
- DAST active mode requires explicit approval and the orchestrator enforces it
- `scripts/integration-test.sh` passes in CI

## Folder layout

```
agents/security/
├── README.md             # this file
├── agent.py              # ClaudeSecurityAgent
├── scanners/
│   ├── __init__.py
│   ├── sbom.py
│   ├── sca.py
│   ├── image.py
│   ├── dast.py
│   ├── secrets.py
│   ├── sign.py
│   └── posture.py
├── prompts/
│   └── hypothesize.md
├── config/               # suppression rules (TBD)
├── scripts/              # dev scripts (see below)
└── tests/                # parser tests with fixtures (TBD)
```
