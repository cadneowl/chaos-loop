"""Tests for security scanner parsers and the runner abstraction."""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.security.runner import (
    FixtureRunner,
    ScannerError,
    ScannerResult,
)
from agents.security.scanners.image import _parse_trivy_image, scan_image
from shared.contracts import FindingSeverity

# ---------------------------------------------------------------------------- #
# FixtureRunner                                                                #
# ---------------------------------------------------------------------------- #


def test_fixture_runner_by_program_match() -> None:
    r = FixtureRunner()
    r.register("trivy", stdout='{"ok": true}', returncode=0)
    result = asyncio.run(r.run(["trivy", "image", "--format", "json", "nginx:latest"]))
    assert result.returncode == 0
    assert result.stdout == '{"ok": true}'
    assert r.calls[-1] == ("trivy", "image", "--format", "json", "nginx:latest")


def test_fixture_runner_exact_match_wins_over_by_program() -> None:
    r = FixtureRunner()
    r.register("trivy", stdout="generic")
    r.register(("trivy", "image", "specific"), stdout="specific")
    result = asyncio.run(r.run(["trivy", "image", "specific"]))
    assert result.stdout == "specific"


def test_fixture_runner_unknown_command_raises() -> None:
    r = FixtureRunner()
    with pytest.raises(ScannerError):
        asyncio.run(r.run(["grype", "foo"]))


def test_scanner_result_is_immutable() -> None:
    """ScannerResult is frozen: callers can't accidentally mutate fixture state."""
    from dataclasses import FrozenInstanceError

    res = ScannerResult(args=("trivy",), stdout="", stderr="", returncode=0)
    with pytest.raises(FrozenInstanceError):
        res.stdout = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------- #
# Trivy parser                                                                 #
# ---------------------------------------------------------------------------- #


_TRIVY_SAMPLE = {
    "SchemaVersion": 2,
    "ArtifactName": "nginx:1.14.0",
    "Results": [
        {
            "Target": "nginx:1.14.0 (debian 9.5)",
            "Class": "os-pkgs",
            "Type": "debian",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2019-9511",
                    "PkgName": "nginx",
                    "InstalledVersion": "1.14.0-0+deb9u3",
                    "FixedVersion": "1.14.2-2+deb9u2",
                    "Severity": "HIGH",
                    "Title": "HTTP/2: large amount of data request leads to DoS",
                    "Description": "Some HTTP/2 implementations are vulnerable...",
                },
                {
                    "VulnerabilityID": "CVE-2018-16843",
                    "PkgName": "nginx",
                    "InstalledVersion": "1.14.0-0+deb9u3",
                    "FixedVersion": "1.14.1-1+deb9u1",
                    "Severity": "MEDIUM",
                    "Title": "Excessive memory consumption via HTTP/2",
                    "Description": "...",
                },
            ],
        },
        {
            "Target": "Dockerfile",
            "Class": "config",
            "Type": "dockerfile",
            "Misconfigurations": [
                {
                    "ID": "DS002",
                    "Title": "Image user should not be root",
                    "Severity": "HIGH",
                    "Description": "Running containers as root is a security risk",
                }
            ],
        },
    ],
}


def test_parse_trivy_returns_one_finding_per_vuln_and_misc() -> None:
    findings = _parse_trivy_image(json.dumps(_TRIVY_SAMPLE), image="nginx:1.14.0")
    assert len(findings) == 3  # 2 vulns + 1 misconfig


def test_parse_trivy_severity_mapping() -> None:
    findings = _parse_trivy_image(json.dumps(_TRIVY_SAMPLE), image="nginx:1.14.0")
    sevs = {f.cve or f.title: f.severity for f in findings}
    assert sevs["CVE-2019-9511"] == FindingSeverity.HIGH
    assert sevs["CVE-2018-16843"] == FindingSeverity.MEDIUM


def test_parse_trivy_extracts_cve_id() -> None:
    findings = _parse_trivy_image(json.dumps(_TRIVY_SAMPLE), image="nginx:1.14.0")
    cves = [f.cve for f in findings if f.cve]
    assert "CVE-2019-9511" in cves
    assert "CVE-2018-16843" in cves


def test_parse_trivy_misconfig_has_no_cve() -> None:
    findings = _parse_trivy_image(json.dumps(_TRIVY_SAMPLE), image="nginx:1.14.0")
    misc = [f for f in findings if "DS002" in f.title]
    assert len(misc) == 1
    assert misc[0].cve is None
    assert misc[0].severity == FindingSeverity.HIGH


def test_parse_trivy_empty_results() -> None:
    findings = _parse_trivy_image(json.dumps({"Results": []}), image="alpine:latest")
    assert findings == []


def test_parse_trivy_results_null() -> None:
    """Trivy emits `"Results": null` when nothing matches; we should handle it."""
    findings = _parse_trivy_image(json.dumps({"Results": None}), image="alpine:latest")
    assert findings == []


def test_parse_trivy_non_json_raises() -> None:
    with pytest.raises(ScannerError, match="not JSON"):
        _parse_trivy_image("oops not json", image="x")


def test_parse_trivy_includes_evidence() -> None:
    findings = _parse_trivy_image(json.dumps(_TRIVY_SAMPLE), image="nginx:1.14.0")
    vuln = next(f for f in findings if f.cve == "CVE-2019-9511")
    assert vuln.evidence["package"] == "nginx"
    assert vuln.evidence["installed_version"] == "1.14.0-0+deb9u3"
    assert vuln.evidence["fixed_version"] == "1.14.2-2+deb9u2"


# ---------------------------------------------------------------------------- #
# scan_image (uses the runner)                                                 #
# ---------------------------------------------------------------------------- #


def test_scan_image_calls_trivy_and_parses() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_SAMPLE))
    findings = asyncio.run(scan_image("nginx:1.14.0", runner=runner))
    assert len(findings) == 3
    # The args invoked include the image at the end.
    last = runner.calls[-1]
    assert last[0] == "trivy"
    assert "nginx:1.14.0" in last


def test_scan_image_raises_on_non_zero_exit() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout="", stderr="cannot pull image", returncode=1)
    with pytest.raises(ScannerError, match="cannot pull image"):
        asyncio.run(scan_image("nope:doesnotexist", runner=runner))


def test_scan_image_severity_arg_passed() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps({"Results": []}))
    asyncio.run(
        scan_image("nginx:1.14.0", runner=runner, severities=("CRITICAL",))
    )
    last = runner.calls[-1]
    # The severities should appear after --severity.
    assert "--severity" in last
    sev_index = last.index("--severity")
    assert last[sev_index + 1] == "CRITICAL"
