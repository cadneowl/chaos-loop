# security/scripts — developer scripts

| Script | Purpose |
|---|---|
| `scan-baseline.sh` | Run the full baseline scan suite against a namespace |
| `scan-verify.sh` | Re-run scans and diff against baseline |
| `drift-check.sh` | Cheap SBOM-only drift check |
| `dast-active.sh` | Run an active ZAP scan — heavy and intrusive, gated |
| `refresh-cve-db.sh` | Update Grype + Trivy CVE databases |
| `suppression-lint.sh` | Validate the suppression YAML |
