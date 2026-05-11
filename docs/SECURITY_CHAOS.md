# Security Chaos Engineering

Security Chaos Engineering (SCE) is the practice of injecting controlled security-relevant failures to surface fragile assumptions in security controls — the same way classical chaos engineering does for availability. Reading: Aaron Rinehart & Kelly Shortridge, *Security Chaos Engineering* (O'Reilly).

In this repo, security is **not a separate workflow**. It flows through the same closed loop:

- The `security/` agent contributes to **baseline** and **post-chaos verification** with the same shape as `tester/`.
- The `chaos/` agent's fault catalogue includes **security-flavored faults**.
- The `diagnostician/` and `fixer/` consume security findings exactly like functional ones.

## Three layers we cover

### 1. Static security testing — runs as part of baseline

| Scanner | What it does | When it runs |
|---|---|---|
| Syft | Generate SBOM for target's container images | Baseline, then again post-chaos for drift |
| Grype | CVE scan against the SBOM | Baseline |
| Trivy | Image scan (vulns + misconfigs) | Baseline |
| gitleaks | Secret scan against the target repo | Baseline |
| cosign | Verify image signatures (Sigstore) | Baseline (fails closed if unsigned) |
| kubescape | K8s posture (NSA/MITRE frameworks, NSA Kubernetes Hardening Guide) | Baseline |

These produce a `SecurityReport` with severity-tagged findings. The orchestrator treats critical+high as **baseline failures** — the system is not in a safe steady state to chaos-test.

### 2. Dynamic security testing — runs during chaos windows

| Scanner | What it does | When it runs |
|---|---|---|
| OWASP ZAP baseline | Crawl + passive scan target HTTP endpoints | During + after chaos windows |
| ZAP active scan (gated) | Active vulnerability probes | Opt-in per experiment, off by default |
| Custom probes | E.g., "is /admin reachable when auth provider is degraded?" | Per security hypothesis |

The intent: classical DAST measures the system at rest. SCE measures it *while it's degraded*. That's where security regressions hide — auth fallback paths that bypass MFA under load, debug endpoints that get exposed when the main router fails, error messages that leak secrets only under specific failure modes.

### 3. Security-flavored faults — injected by the chaos agent

These extend the Chaos Mesh native catalogue. Each is implemented in `agents/chaos/faults/`.

| Fault | Hypothesis it tests | Implementation |
|---|---|---|
| `cert.revoke` | Cert revocation triggers graceful failover, not outage | NetworkChaos to block OCSP/CRL; manual cert deletion in test cluster |
| `cert.expire` | Near-expiry certs trigger alerts before failure | Time skew via TimeChaos |
| `tls.downgrade` | App refuses to fall back to TLS 1.0 or plaintext | NetworkChaos rewriting cipher list at the proxy |
| `auth.outage` | App fails closed when auth provider is down | NetworkChaos blocking egress to IdP |
| `auth.latency` | App doesn't open a side channel under auth latency | NetworkChaos delay on IdP path |
| `secret.rotate` | App handles mid-flight secret rotation without restart | Patch secret + observe |
| `image.swap_vuln` | Admission policy rejects a known-vulnerable image | Deploy image with planted CVE; expect rejection |
| `image.swap_unsigned` | Admission policy rejects unsigned image | Deploy unsigned image; expect rejection |
| `iam.degrade` | App handles partial IAM (e.g., 50% of role assumes fail) | NetworkChaos partial loss on STS |
| `netpol.regress` | Removing a NetworkPolicy doesn't silently open paths | Delete policy; verify deny still enforced at app layer |
| `egress.exfil_sim` | Simulated exfil attempt is detected by runtime sensors | Curl to known-bad domain from within pod; expect alert |
| `runtime.tamper` | Container drift (writable rootfs use) is detected | Falco / runtime sensor check |

Each fault declares `requires_approval: true` if it could be confused with a real incident by your on-call. See `agents/chaos/faults/_meta.py`.

## Security hypotheses

The `security/` agent generates hypotheses by reading:
- Recent CVEs in the SBOM (suggests "what if this CVE-affected service is exercised under chaos?")
- Target's auth/secrets code paths (suggests `auth.*` and `secret.*` faults)
- NetworkPolicies and IAM policies (suggests `netpol.regress` and `iam.degrade`)

Example output (Pydantic model `SecurityHypothesis`):

```python
SecurityHypothesis(
    id="sh-auth-fallback-2026-001",
    statement="The cart service must NOT serve any authenticated route when the IdP is unreachable.",
    rationale="cart-service handler at services/cart/auth_middleware.py:42 has a `_dev_bypass` path guarded by env var; confirm it is not active in staging.",
    proposed_fault="auth.outage",
    success_criteria=[
        "ZAP active scan against /api/cart returns 401/503 for all paths during fault window",
        "No request reaches the cart handler with `user_id` set during fault window (verify via traces)",
    ],
    confidence=0.7,
    references=["services/cart/auth_middleware.py:42"],
)
```

## What we deliberately do NOT do

- **No exploitation of real CVEs against production-shaped services.** We use synthetic vulnerable images in `target/security-fixtures/` for the `image.swap_vuln` fault.
- **No credential harvesting.** Even simulated exfil targets are to a controlled sink, not real third parties.
- **No purple-team-as-default.** Security faults that resemble real attacks (`egress.exfil_sim`, `runtime.tamper`) require explicit approval in the experiment YAML, **and** that the target cluster be flagged `chaos.kosta.dev/security-allowed: "true"`. Otherwise the orchestrator refuses.

## Where the line lives between this and a pentest

A pentest is goal-driven ("can you get domain admin?"). SCE is hypothesis-driven ("the system maintains property X under failure Y"). They complement each other; this repo does the second.
