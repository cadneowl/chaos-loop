# Security Policy

This project is a security tool — it scans codebases for fragilities and
injects faults into running clusters. We take vulnerability reports
seriously and ask that you report responsibly.

## Reporting a vulnerability

**Please do not file public GitHub issues for security problems.**

Use GitHub's Private Vulnerability Reporting:

1. Go to the [Security tab](https://github.com/cadneowl/chaos-loop/security)
2. Click **Report a vulnerability**
3. Fill in the form

Or email the maintainer at the address in `git log` for `master`. PGP is
not currently published; if you need encrypted reporting, mention that in
your first message and we'll arrange a key exchange.

## What to include

- A short description of the issue
- Steps to reproduce (a minimal repro is gold)
- Affected commit / version
- Your assessment of severity and impact (we'll re-grade)
- Whether you've notified anyone else

## What to expect

| Stage | Target |
|---|---|
| Acknowledgement | within 72 hours |
| Initial assessment + severity grade | within 7 days |
| Fix or mitigation plan | within 30 days for high / critical findings |
| Public disclosure | coordinated; default 90 days after report |

We'll credit the reporter in the release notes unless asked to keep the
report anonymous.

## In scope

- Command injection, path traversal, or other RCE in the orchestrator or
  scanner wrappers (`agents/security/scanners/`)
- Secrets leaking through the audit log, finding evidence, or
  `ExperimentRecord` persistence
- Bypasses of the [safety gates](docs/SAFETY.md): cluster denylist,
  blast-radius checks, namespace annotation, budget enforcement
- Privilege escalation in the meta-harness or agent factory
- Supply-chain issues in our pinned dependencies that affect runtime
  trust (e.g., a malicious upstream we vendored)

## Out of scope

- Bugs that require an already-compromised cluster + kubeconfig to exploit
  (the threat model assumes a trusted operator)
- Findings produced by chaos against a target system — those are
  **outputs** of the loop, not vulnerabilities in this tool
- Denial-of-service against the orchestrator itself (it's a CLI; a
  malicious operator submitting a runaway plan is the same as a
  malicious operator deleting their own files)
- Issues in upstream tools we wrap (Chaos Mesh, Trivy, Syft, Grype,
  gitleaks, cosign, kubescape) — please report those to their respective
  projects

## Hardening notes

If you're deploying this tool, the following matter most:

- **Restrict who can run `chaos run`** — the orchestrator can spawn pod
  kills, network partitions, and `gh pr create`. Treat the operator as
  privileged.
- **Set the `forbidden_cluster_substrings`** to cover every production
  context name you use. The default (`prod`, `production`, `live`,
  `main`) is a sane floor, not a complete list.
- **Require namespace annotations.** The default
  `require_namespace_annotation: true` is load-bearing — turning it off
  silently widens the blast radius.
- **Treat LLM API keys as production secrets.** A hybrid / llm-profile
  run with a leaked key can rack up cost.
- **Audit `experiments/runs/<id>/proposed/`** before applying any fix
  PR. The fixer is sandboxed against a path denylist but is not the
  last line of defense — the reviewer is.
