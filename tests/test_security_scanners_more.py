"""Tests for the five M4.1 scanner wrappers: Syft, Grype, gitleaks, cosign, kubescape.

Each scanner is tested via its parser (canned bytes) + its end-to-end path
through a FixtureRunner. Real binary invocation is not exercised here —
that's covered by the integration scripts under ``scripts/``.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.security.runner import FixtureRunner, ScannerError
from agents.security.scanners.posture import _parse_kubescape, scan_namespace
from agents.security.scanners.sbom import _digest_sbom, generate_sbom
from agents.security.scanners.sca import _parse_grype, scan_sbom
from agents.security.scanners.sca import scan_image as grype_scan_image
from agents.security.scanners.secrets import _parse_gitleaks, scan_repo
from agents.security.scanners.sign import verify_image
from shared.contracts import FindingSeverity

# --------------------------------------------------------------------------- #
# Syft (SBOM)                                                                 #
# --------------------------------------------------------------------------- #


_SYFT_SAMPLE = {
    "spdxVersion": "SPDX-2.3",
    "creationInfo": {"created": "2026-05-12T10:00:00Z"},  # noise — must NOT affect digest
    "packages": [
        {"name": "openssl", "versionInfo": "1.1.1k"},
        {"name": "nginx", "versionInfo": "1.27.0"},
        {"name": "libc", "versionInfo": "2.36"},
    ],
}


def test_digest_sbom_ignores_creation_timestamp() -> None:
    """Two SBOMs with the same packages but different creation timestamps
    must produce the same digest. Otherwise drift detection is useless."""
    a = {**_SYFT_SAMPLE, "creationInfo": {"created": "2026-05-12T10:00:00Z"}}
    b = {**_SYFT_SAMPLE, "creationInfo": {"created": "2026-05-13T11:00:00Z"}}
    assert _digest_sbom(a) == _digest_sbom(b)


def test_digest_sbom_changes_when_packages_change() -> None:
    """Adding a package must change the digest."""
    a = {"packages": [{"name": "x", "versionInfo": "1"}]}
    b = {"packages": [{"name": "x", "versionInfo": "1"}, {"name": "y", "versionInfo": "2"}]}
    assert _digest_sbom(a) != _digest_sbom(b)


def test_digest_sbom_changes_when_version_changes() -> None:
    a = {"packages": [{"name": "openssl", "versionInfo": "1.1.1k"}]}
    b = {"packages": [{"name": "openssl", "versionInfo": "1.1.1l"}]}
    assert _digest_sbom(a) != _digest_sbom(b)


def test_digest_sbom_handles_empty_or_missing_packages() -> None:
    assert _digest_sbom({}).startswith("sha256:")
    assert _digest_sbom({"packages": []}) == _digest_sbom({"packages": []})


def test_digest_sbom_is_order_invariant() -> None:
    """Package ordering must not matter — Syft can emit either order."""
    a = {"packages": [{"name": "a", "versionInfo": "1"}, {"name": "b", "versionInfo": "2"}]}
    b = {"packages": [{"name": "b", "versionInfo": "2"}, {"name": "a", "versionInfo": "1"}]}
    assert _digest_sbom(a) == _digest_sbom(b)


def test_generate_sbom_returns_empty_findings_and_digest() -> None:
    runner = FixtureRunner()
    runner.register("syft", stdout=json.dumps(_SYFT_SAMPLE))
    findings, digest, sbom = asyncio.run(generate_sbom("nginx:1.27", runner=runner))
    assert findings == []
    assert digest.startswith("sha256:")
    assert sbom["packages"][0]["name"] == "openssl"


def test_generate_sbom_invokes_syft_with_correct_args() -> None:
    runner = FixtureRunner()
    runner.register("syft", stdout=json.dumps(_SYFT_SAMPLE))
    asyncio.run(generate_sbom("nginx:1.27", runner=runner))
    args = runner.calls[-1]
    assert args[0] == "syft"
    assert "nginx:1.27" in args
    assert "spdx-json" in args


def test_generate_sbom_raises_on_non_zero_exit() -> None:
    runner = FixtureRunner()
    runner.register("syft", stdout="", stderr="manifest unknown", returncode=1)
    with pytest.raises(ScannerError, match="manifest unknown"):
        asyncio.run(generate_sbom("nope:doesnotexist", runner=runner))


def test_generate_sbom_raises_on_bad_json() -> None:
    runner = FixtureRunner()
    runner.register("syft", stdout="not-json")
    with pytest.raises(ScannerError, match="was not JSON"):
        asyncio.run(generate_sbom("img:tag", runner=runner))


# --------------------------------------------------------------------------- #
# Grype (SCA)                                                                 #
# --------------------------------------------------------------------------- #


_GRYPE_SAMPLE = {
    "matches": [
        {
            "vulnerability": {
                "id": "CVE-2023-12345",
                "severity": "High",
                "description": "Critical buffer overflow in libfoo",
                "dataSource": "https://nvd.nist.gov/vuln/detail/CVE-2023-12345",
                "fix": {"versions": ["1.2.3"]},
            },
            "artifact": {"name": "libfoo", "version": "1.2.0", "type": "deb"},
        },
        {
            "vulnerability": {
                "id": "GHSA-xxxx-yyyy-zzzz",
                "severity": "Medium",
                "description": "Less critical issue",
                "fix": {"versions": []},
            },
            "artifact": {"name": "libbar", "version": "2.0.0", "type": "apk"},
        },
    ]
}


def test_parse_grype_emits_one_finding_per_match() -> None:
    findings = _parse_grype(json.dumps(_GRYPE_SAMPLE), location="nginx:1.27")
    assert len(findings) == 2


def test_parse_grype_maps_severity() -> None:
    findings = _parse_grype(json.dumps(_GRYPE_SAMPLE), location="x")
    by_cve = {f.cve or f.title: f.severity for f in findings}
    assert by_cve["CVE-2023-12345"] == FindingSeverity.HIGH
    # GHSA ids aren't CVEs; they appear in the title, not the cve field.
    ghsa_title = next(t for t in by_cve if "GHSA" in t)
    assert by_cve[ghsa_title] == FindingSeverity.MEDIUM


def test_parse_grype_only_marks_cve_when_id_starts_with_cve() -> None:
    findings = _parse_grype(json.dumps(_GRYPE_SAMPLE), location="x")
    cves = [f.cve for f in findings]
    assert "CVE-2023-12345" in cves
    assert None in cves  # the GHSA one


def test_parse_grype_includes_fix_versions_in_evidence() -> None:
    findings = _parse_grype(json.dumps(_GRYPE_SAMPLE), location="x")
    vuln = next(f for f in findings if f.cve == "CVE-2023-12345")
    assert vuln.evidence["fixed_versions"] == ["1.2.3"]
    assert vuln.evidence["package"] == "libfoo"
    assert vuln.evidence["installed_version"] == "1.2.0"


def test_parse_grype_empty_matches() -> None:
    assert _parse_grype(json.dumps({"matches": []}), location="x") == []


def test_parse_grype_non_json_raises() -> None:
    with pytest.raises(ScannerError, match="was not JSON"):
        _parse_grype("not json", location="x")


def test_grype_scan_image_calls_grype_correctly() -> None:
    runner = FixtureRunner()
    runner.register("grype", stdout=json.dumps(_GRYPE_SAMPLE))
    findings = asyncio.run(grype_scan_image("nginx:1.27", runner=runner))
    assert len(findings) == 2
    args = runner.calls[-1]
    assert args[0] == "grype"
    assert "nginx:1.27" in args


def test_grype_scan_sbom_uses_sbom_prefix() -> None:
    runner = FixtureRunner()
    runner.register("grype", stdout=json.dumps({"matches": []}))
    asyncio.run(scan_sbom("/tmp/sbom.json", runner=runner))
    args = runner.calls[-1]
    assert any(a.startswith("sbom:") for a in args)


# --------------------------------------------------------------------------- #
# gitleaks                                                                    #
# --------------------------------------------------------------------------- #


_GITLEAKS_SAMPLE = [
    {
        "RuleID": "aws-access-token",
        "Description": "AWS Access Token",
        "File": "src/config.py",
        "StartLine": 42,
        "Commit": "abc123",
        "Secret": "AKIA...",
    },
    {
        "RuleID": "generic-api-key",
        "Description": "Generic API Key",
        "File": "tests/fixtures/keys.json",
        "StartLine": 5,
        "Commit": "def456",
    },
]


def test_parse_gitleaks_emits_one_finding_per_match() -> None:
    findings = _parse_gitleaks(json.dumps(_GITLEAKS_SAMPLE), location="/repo")
    assert len(findings) == 2


def test_parse_gitleaks_all_findings_are_high_severity() -> None:
    findings = _parse_gitleaks(json.dumps(_GITLEAKS_SAMPLE), location="/repo")
    assert all(f.severity == FindingSeverity.HIGH for f in findings)


def test_parse_gitleaks_evidence_pinpoints_file_and_line() -> None:
    findings = _parse_gitleaks(json.dumps(_GITLEAKS_SAMPLE), location="/repo")
    aws = next(f for f in findings if "aws" in f.evidence["rule_id"])
    assert aws.evidence["file"] == "src/config.py"
    assert aws.evidence["start_line"] == 42


def test_parse_gitleaks_does_not_embed_actual_secret() -> None:
    """The matched secret string must NOT propagate into the finding.

    gitleaks always reports the matched substring; we mustn't put it in
    description/evidence where it'd land in the SQLite store + logs."""
    findings = _parse_gitleaks(json.dumps(_GITLEAKS_SAMPLE), location="/repo")
    for f in findings:
        assert "AKIA" not in f.description
        assert "AKIA" not in str(f.evidence)


def test_parse_gitleaks_handles_empty_output() -> None:
    """Clean scans may print empty stdout or `[]`."""
    assert _parse_gitleaks("", location="/repo") == []
    assert _parse_gitleaks("[]", location="/repo") == []


def test_parse_gitleaks_accepts_findings_wrapper() -> None:
    """Some gitleaks versions wrap in {"findings": [...]} instead of bare array."""
    payload = json.dumps({"findings": _GITLEAKS_SAMPLE})
    findings = _parse_gitleaks(payload, location="/repo")
    assert len(findings) == 2


def test_parse_gitleaks_non_json_raises() -> None:
    with pytest.raises(ScannerError):
        _parse_gitleaks("nope", location="/repo")


def test_scan_repo_treats_rc1_as_findings_not_error() -> None:
    """gitleaks exits 1 when leaks ARE found — that's signal, not failure."""
    runner = FixtureRunner()
    runner.register("gitleaks", stdout=json.dumps(_GITLEAKS_SAMPLE), returncode=1)
    findings = asyncio.run(scan_repo("/repo", runner=runner))
    assert len(findings) == 2


def test_scan_repo_treats_rc2_as_error() -> None:
    """rc >= 2 means a tool-level failure (config invalid, can't read path)."""
    runner = FixtureRunner()
    runner.register("gitleaks", stdout="", stderr="config not found", returncode=2)
    with pytest.raises(ScannerError, match="config not found"):
        asyncio.run(scan_repo("/repo", runner=runner))


# --------------------------------------------------------------------------- #
# cosign                                                                      #
# --------------------------------------------------------------------------- #


def test_verify_image_returns_empty_on_success() -> None:
    runner = FixtureRunner()
    runner.register("cosign", stdout="Verified ok", returncode=0)
    findings = asyncio.run(
        verify_image("nginx:1.27", public_key="/keys/pub.pem", runner=runner)
    )
    assert findings == []


def test_verify_image_emits_critical_finding_on_failure() -> None:
    runner = FixtureRunner()
    runner.register(
        "cosign",
        stdout="",
        stderr="no signatures found for image",
        returncode=1,
    )
    findings = asyncio.run(
        verify_image("nginx:1.27", public_key="/keys/pub.pem", runner=runner)
    )
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.CRITICAL
    assert findings[0].scanner == "cosign"
    assert "no signatures" in findings[0].description


def test_verify_image_passes_key_in_args() -> None:
    runner = FixtureRunner()
    runner.register("cosign", returncode=0)
    asyncio.run(verify_image("nginx:1.27", public_key="/keys/pub.pem", runner=runner))
    args = runner.calls[-1]
    assert "--key" in args
    assert "/keys/pub.pem" in args


def test_verify_image_supports_keyless_mode() -> None:
    runner = FixtureRunner()
    runner.register("cosign", returncode=0)
    asyncio.run(
        verify_image(
            "nginx:1.27",
            certificate_identity="user@example.com",
            certificate_oidc_issuer="https://accounts.google.com",
            runner=runner,
        )
    )
    args = runner.calls[-1]
    assert "--certificate-identity" in args
    assert "--certificate-oidc-issuer" in args


def test_verify_image_requires_a_trust_mode() -> None:
    """Without a key or keyless config we refuse — don't infer."""
    runner = FixtureRunner()
    with pytest.raises(ValueError, match="public_key OR"):
        asyncio.run(verify_image("nginx:1.27", runner=runner))


def test_verify_image_raises_when_cosign_binary_missing() -> None:
    """Missing binary is a tool error, not a policy finding."""
    runner = FixtureRunner()
    runner.register(
        "cosign", stdout="", stderr="executable file not found in $PATH", returncode=127
    )
    with pytest.raises(ScannerError, match="cosign binary not available"):
        asyncio.run(
            verify_image("nginx:1.27", public_key="/keys/pub.pem", runner=runner)
        )


# --------------------------------------------------------------------------- #
# kubescape                                                                   #
# --------------------------------------------------------------------------- #


_KUBESCAPE_SAMPLE = {
    "summaryDetails": {
        "controls": {
            "C-0001": {
                "name": "Forbidden Container Registries",
                "description": "Restrict pulling images to approved registries",
                "status": {"status": "failed"},
                "severity": 7,
                "resourceIDs": ["apps/v1/Deployment/checkout"],
            },
            "C-0017": {
                "name": "Immutable container filesystem",
                "description": "Containers should not run with a writable rootfs",
                "status": {"status": "failed"},
                "severity": 5,
                "resourceIDs": [
                    "apps/v1/Deployment/cart",
                    "apps/v1/Deployment/recommendation",
                ],
            },
            "C-0030": {
                "name": "Ingress and Egress blocked",
                "status": {"status": "passed"},
                "severity": 6,
            },
        }
    }
}


def test_parse_kubescape_only_emits_failed_controls() -> None:
    findings = _parse_kubescape(
        json.dumps(_KUBESCAPE_SAMPLE), namespace="otel-demo", framework="nsa"
    )
    # 2 failed controls; the passed one is filtered out.
    assert len(findings) == 2
    titles = {f.title for f in findings}
    assert any("C-0001" in t for t in titles)
    assert any("C-0017" in t for t in titles)


def test_parse_kubescape_maps_numeric_severity() -> None:
    findings = _parse_kubescape(
        json.dumps(_KUBESCAPE_SAMPLE), namespace="otel-demo", framework="nsa"
    )
    by_control = {f.evidence["control_id"]: f.severity for f in findings}
    assert by_control["C-0001"] == FindingSeverity.HIGH  # severity 7
    assert by_control["C-0017"] == FindingSeverity.MEDIUM  # severity 5


def test_parse_kubescape_lists_affected_resources_in_evidence() -> None:
    findings = _parse_kubescape(
        json.dumps(_KUBESCAPE_SAMPLE), namespace="otel-demo", framework="nsa"
    )
    c17 = next(f for f in findings if f.evidence["control_id"] == "C-0017")
    assert "apps/v1/Deployment/cart" in c17.evidence["affected_resource_ids"]
    assert c17.evidence["framework"] == "nsa"


def test_parse_kubescape_caps_affected_resource_list() -> None:
    """Long affected-resources lists must be truncated to keep the report compact."""
    big = {
        "summaryDetails": {
            "controls": {
                "C-X": {
                    "name": "x",
                    "status": {"status": "failed"},
                    "severity": 5,
                    "resourceIDs": [f"r/{i}" for i in range(200)],
                }
            }
        }
    }
    findings = _parse_kubescape(json.dumps(big), namespace="ns", framework="nsa")
    assert len(findings[0].evidence["affected_resource_ids"]) == 50


def test_parse_kubescape_handles_empty_or_missing_controls() -> None:
    assert _parse_kubescape("{}", namespace="ns", framework="nsa") == []
    assert _parse_kubescape(json.dumps({"summaryDetails": {}}), namespace="ns", framework="nsa") == []


def test_parse_kubescape_handles_warning_status() -> None:
    """Warnings are weaker than failures but we still surface them."""
    warn = {
        "summaryDetails": {
            "controls": {
                "C-W": {
                    "name": "w",
                    "status": {"status": "warning"},
                    "severity": 3,
                }
            }
        }
    }
    findings = _parse_kubescape(json.dumps(warn), namespace="ns", framework="nsa")
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.LOW


def test_scan_namespace_passes_framework_arg() -> None:
    runner = FixtureRunner()
    runner.register("kubescape", stdout=json.dumps(_KUBESCAPE_SAMPLE))
    asyncio.run(scan_namespace("otel-demo", framework="mitre", runner=runner))
    args = runner.calls[-1]
    assert "framework" in args
    assert "mitre" in args
    assert "--include-namespaces" in args
    assert "otel-demo" in args
