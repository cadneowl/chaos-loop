"""Grype wrapper: CVE scan against a container image or stored SBOM.

Reference: https://github.com/anchore/grype

Why both image and SBOM modes? Re-scanning the image from scratch is slow.
Once Syft has produced an SBOM, Grype can scan the SBOM directly via
``grype sbom:<path>``, which is significantly cheaper and avoids re-pulling.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.security.runner import ScannerError, ScannerRunner, SubprocessRunner
from shared.contracts import FindingSeverity, SecurityFinding

_SEVERITY_MAP = {
    "CRITICAL": FindingSeverity.CRITICAL,
    "HIGH": FindingSeverity.HIGH,
    "MEDIUM": FindingSeverity.MEDIUM,
    "LOW": FindingSeverity.LOW,
    "NEGLIGIBLE": FindingSeverity.INFO,
    "UNKNOWN": FindingSeverity.INFO,
}


async def scan_image(
    image: str,
    *,
    runner: ScannerRunner | None = None,
    timeout_seconds: float = 240.0,
) -> list[SecurityFinding]:
    """Scan a container image directly. Slower than SBOM mode but no setup."""
    return await _scan(["grype", image, "-o", "json", "-q"], runner, timeout_seconds, image)


async def scan_sbom(
    sbom_path: str | Path,
    *,
    runner: ScannerRunner | None = None,
    timeout_seconds: float = 120.0,
) -> list[SecurityFinding]:
    """Scan a stored SBOM (e.g., from Syft). Use this after a baseline SBOM."""
    return await _scan(
        ["grype", f"sbom:{sbom_path}", "-o", "json", "-q"],
        runner, timeout_seconds, str(sbom_path),
    )


async def _scan(
    args: list[str],
    runner: ScannerRunner | None,
    timeout_seconds: float,
    location: str,
) -> list[SecurityFinding]:
    runner = runner or SubprocessRunner()
    result = await runner.run(args, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise ScannerError(
            f"grype exit {result.returncode} for {location}: {result.stderr[:200]}"
        )
    return _parse_grype(result.stdout, location=location)


def _parse_grype(stdout: str, *, location: str) -> list[SecurityFinding]:
    """Parse ``grype -o json`` output.

    Shape:
        { "matches": [
            { "vulnerability": {"id": "CVE-...", "severity": "High", ...},
              "artifact": {"name": "openssl", "version": "1.1.1", "type": "deb"},
              "matchDetails": [...] },
            ...
          ]
        }
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ScannerError(f"grype stdout was not JSON: {e}") from e

    out: list[SecurityFinding] = []
    for match in payload.get("matches") or []:
        vuln = match.get("vulnerability") or {}
        artifact = match.get("artifact") or {}
        cve = vuln.get("id", "UNKNOWN")
        pkg = artifact.get("name", "?")
        sev = _SEVERITY_MAP.get(vuln.get("severity", "").upper(), FindingSeverity.INFO)
        # Grype's `fix` field has the upgrade target if one is available.
        fix_versions = (vuln.get("fix") or {}).get("versions") or []
        out.append(
            SecurityFinding(
                id=f"f-grype-{cve}-{pkg}".lower().replace("_", "-"),
                severity=sev,
                title=f"{cve} in {pkg}",
                description=vuln.get("description") or "",
                scanner="grype",
                cve=cve if cve.startswith("CVE-") else None,
                evidence={
                    "package": pkg,
                    "installed_version": artifact.get("version"),
                    "fixed_versions": fix_versions,
                    "type": artifact.get("type"),
                    "data_source": vuln.get("dataSource"),
                },
                location=location,
            )
        )
    return out
