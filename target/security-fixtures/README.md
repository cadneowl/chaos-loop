# target/security-fixtures

**Synthetic** images used by security-flavored faults. These are NOT real exploitation payloads; they are fixtures that satisfy detectors (a CVE-bearing base image to test admission policy; an unsigned image to test cosign verification) without doing anything harmful.

## Files

- `Dockerfile.vuln` — image with an intentionally-old base (e.g., `alpine:3.12`) carrying known CVEs that Grype/Trivy detect. Used by `image.swap_vuln` fault.
- `Dockerfile.unsigned` — image that is NOT signed by the project's cosign key. Used by `image.swap_unsigned` fault.

## Building (milestone 4)

```bash
docker build -f Dockerfile.vuln -t local/chaos-fixture-vuln:0.1 .
kind load docker-image local/chaos-fixture-vuln:0.1 --name chaos

docker build -f Dockerfile.unsigned -t local/chaos-fixture-unsigned:0.1 .
kind load docker-image local/chaos-fixture-unsigned:0.1 --name chaos
```

## Hard rules

- Never push these to a public registry.
- Never use a real (recent) RCE-grade CVE as the planted vulnerability — a high-CVSS dependency vuln in a non-network-facing path is enough.
- These images must serve a static "I am a chaos fixture" HTTP response and nothing else, in case they are ever inadvertently exposed.
