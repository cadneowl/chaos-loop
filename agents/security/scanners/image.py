"""Trivy wrapper: image vulnerability + misconfiguration scan."""

from __future__ import annotations

import json

from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from shared.contracts import FindingSeverity, SecurityFinding

_SEVERITY_MAP = {
    "CRITICAL": FindingSeverity.CRITICAL,
    "HIGH": FindingSeverity.HIGH,
    "MEDIUM": FindingSeverity.MEDIUM,
    "LOW": FindingSeverity.LOW,
    "UNKNOWN": FindingSeverity.INFO,
    "NEGLIGIBLE": FindingSeverity.INFO,
}

# Default severities to scan for; tunable per call. We exclude LOW by default
# to keep signal-to-noise reasonable. Operators can override.
_DEFAULT_SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM")


async def scan_image(
    image: str,
    *,
    runner: ScannerRunner | None = None,
    severities: tuple[str, ...] = _DEFAULT_SEVERITIES,
    timeout_seconds: float = 180.0,
) -> list[SecurityFinding]:
    """Scan a container image for vulnerabilities + misconfigurations via Trivy."""
    runner = runner or SubprocessRunner()
    args = [
        "trivy",
        "image",
        "--format",
        "json",
        "--severity",
        ",".join(severities),
        "--quiet",
        image,
    ]
    result = await runner.run(args, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise ScannerError(
            f"trivy exit {result.returncode} for {image}: {result.stderr[:200]}"
        )
    return _parse_trivy_image(result.stdout, image=image)


def _parse_trivy_image(stdout: str, *, image: str) -> list[SecurityFinding]:
    """Parse Trivy's `image --format json` output into SecurityFinding list."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ScannerError(f"trivy stdout was not JSON: {e}") from e

    findings: list[SecurityFinding] = []
    for result in payload.get("Results") or []:
        target = result.get("Target", image)

        for vuln in result.get("Vulnerabilities") or []:
            cve = vuln.get("VulnerabilityID", "UNKNOWN")
            pkg = vuln.get("PkgName", "?")
            sev = _SEVERITY_MAP.get(vuln.get("Severity", "").upper(), FindingSeverity.INFO)
            findings.append(
                SecurityFinding(
                    id=f"f-trivy-{cve}-{pkg}".lower().replace("_", "-"),
                    severity=sev,
                    title=f"{cve} in {pkg}",
                    description=vuln.get("Title") or vuln.get("Description") or "",
                    scanner="trivy",
                    cve=cve if cve.startswith("CVE-") else None,
                    evidence={
                        "package": pkg,
                        "installed_version": vuln.get("InstalledVersion"),
                        "fixed_version": vuln.get("FixedVersion"),
                        "target": target,
                    },
                    location=image,
                )
            )

        for misc in result.get("Misconfigurations") or []:
            mid = misc.get("ID", "MISC")
            sev = _SEVERITY_MAP.get(misc.get("Severity", "").upper(), FindingSeverity.INFO)
            findings.append(
                SecurityFinding(
                    id=f"f-trivy-misc-{mid}".lower().replace("_", "-"),
                    severity=sev,
                    title=f"{mid}: {misc.get('Title', 'misconfiguration')}",
                    description=misc.get("Description") or "",
                    scanner="trivy",
                    cve=None,
                    evidence={"target": target, "type": misc.get("Type")},
                    location=image,
                )
            )

    return findings
