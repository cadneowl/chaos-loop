"""Tests for ClaudeSecurityAgent.baseline() against FixtureRunner."""

from __future__ import annotations

import asyncio
import json

from agents.security.agent import ClaudeSecurityAgent
from agents.security.runner import FixtureRunner
from shared.contracts import FindingSeverity, SecurityRequest

_TRIVY_TWO_VULNS = {
    "Results": [
        {
            "Target": "x:latest",
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2024-0001",
                    "PkgName": "openssl",
                    "Severity": "CRITICAL",
                    "Title": "ossl issue",
                },
                {
                    "VulnerabilityID": "CVE-2024-0002",
                    "PkgName": "openssl",
                    "Severity": "MEDIUM",
                    "Title": "minor ossl issue",
                },
            ],
        }
    ]
}

_TRIVY_NO_VULNS: dict = {"Results": []}


def _req(*images: str) -> SecurityRequest:
    return SecurityRequest(
        kind="baseline",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app="otel-demo",
        target_images=list(images),
    )


def _agent_with(runner: FixtureRunner, *, skip_errors: bool = True) -> ClaudeSecurityAgent:
    return ClaudeSecurityAgent(runner=runner, skip_on_scanner_error=skip_errors)


# ---------------------------------------------------------------------------- #
# baseline                                                                     #
# ---------------------------------------------------------------------------- #


def test_baseline_no_images_yields_empty_findings() -> None:
    agent = _agent_with(FixtureRunner())
    report = asyncio.run(agent.baseline(_req()))
    assert report.findings == []
    assert report.has_critical_or_high is False
    assert report.request_kind == "baseline"


def test_baseline_single_image_aggregates_findings() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_TWO_VULNS))
    report = asyncio.run(_agent_with(runner).baseline(_req("x:latest")))
    assert len(report.findings) == 2
    sevs = {f.severity for f in report.findings}
    assert FindingSeverity.CRITICAL in sevs
    assert FindingSeverity.MEDIUM in sevs


def test_baseline_critical_finding_flips_high_or_critical_flag() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_TWO_VULNS))
    report = asyncio.run(_agent_with(runner).baseline(_req("x:latest")))
    assert report.has_critical_or_high is True


def test_baseline_clean_image_passes() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    report = asyncio.run(_agent_with(runner).baseline(_req("clean:latest")))
    assert report.findings == []
    assert report.has_critical_or_high is False


def test_baseline_multiple_images_aggregates() -> None:
    """Each image is scanned independently; findings accumulate."""
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_TWO_VULNS))
    report = asyncio.run(_agent_with(runner).baseline(_req("a:1", "b:2", "c:3")))
    # 3 images * 2 vulns each = 6 findings.
    assert len(report.findings) == 6
    # Trivy was called once per image.
    assert sum(1 for c in runner.calls if c[0] == "trivy") == 3


def test_baseline_skips_failing_image_by_default() -> None:
    """A single scan failure shouldn't abort the whole baseline."""
    runner = FixtureRunner()
    runner.register("trivy", stdout="bad-not-json", returncode=0)  # parse error
    report = asyncio.run(_agent_with(runner, skip_errors=True).baseline(_req("a:1")))
    assert report.findings == []


def test_baseline_propagates_error_when_skip_disabled() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout="bad-not-json", returncode=0)
    import pytest

    from agents.security.runner import ScannerError

    with pytest.raises(ScannerError):
        asyncio.run(_agent_with(runner, skip_errors=False).baseline(_req("a:1")))


# ---------------------------------------------------------------------------- #
# verify                                                                       #
# ---------------------------------------------------------------------------- #


def test_verify_runs_same_scanners() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_TWO_VULNS))
    report = asyncio.run(
        _agent_with(runner).verify(
            SecurityRequest(
                kind="verify",
                experiment_id="exp-aaaaaaaaaaaa",
                target_app="otel-demo",
                target_images=["x:latest"],
            )
        )
    )
    assert report.request_kind == "verify"
    assert len(report.findings) == 2


# ---------------------------------------------------------------------------- #
# M4.1 scanner orchestration                                                   #
# ---------------------------------------------------------------------------- #

from agents.security.agent import SecurityScanConfig  # noqa: E402

_SYFT_BASELINE = {
    "packages": [
        {"name": "openssl", "versionInfo": "1.1.1k"},
        {"name": "nginx", "versionInfo": "1.27.0"},
    ]
}
_SYFT_DRIFTED = {
    "packages": [
        {"name": "openssl", "versionInfo": "1.1.1k"},
        {"name": "nginx", "versionInfo": "1.27.0"},
        {"name": "curl", "versionInfo": "8.0"},  # NEW package — drift signal
    ]
}


def test_only_trivy_runs_by_default() -> None:
    """Default config keeps the dependency surface minimal."""
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    asyncio.run(_agent_with(runner).baseline(_req("x:latest")))
    scanners = {c[0] for c in runner.calls}
    assert scanners == {"trivy"}


def test_enabling_syft_records_baseline_sbom_digest() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    runner.register("syft", stdout=json.dumps(_SYFT_BASELINE))

    config = SecurityScanConfig(enable_trivy=True, enable_syft=True)
    agent = ClaudeSecurityAgent(runner=runner, config=config)
    report = asyncio.run(agent.baseline(_req("x:latest")))

    assert report.sbom_digest is not None
    assert report.sbom_digest.startswith("sha256:")
    assert report.sbom_drift_from_baseline is False


def test_drift_detection_flips_when_sbom_changes() -> None:
    """baseline -> verify with a different SBOM should set drift=True."""
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    # First scan emits the baseline SBOM; second emits the drifted one.
    # FixtureRunner returns the most-recent registration for "syft", so we
    # do baseline -> register-new -> verify.
    runner.register("syft", stdout=json.dumps(_SYFT_BASELINE))
    config = SecurityScanConfig(enable_trivy=True, enable_syft=True)
    agent = ClaudeSecurityAgent(runner=runner, config=config)

    req = SecurityRequest(
        kind="baseline",
        experiment_id="exp-bbbbbbbbbbbb",
        target_app="otel-demo",
        target_images=["x:latest"],
    )
    baseline_report = asyncio.run(agent.baseline(req))
    assert baseline_report.sbom_drift_from_baseline is False

    # New package appears between baseline and verify (e.g., the chaos run
    # caused a deploy with an extra dep). Re-register syft's reply.
    runner.register("syft", stdout=json.dumps(_SYFT_DRIFTED))

    verify_report = asyncio.run(agent.verify(
        SecurityRequest(
            kind="verify",
            experiment_id=req.experiment_id,
            target_app="otel-demo",
            target_images=["x:latest"],
        )
    ))
    assert verify_report.sbom_drift_from_baseline is True
    # Digest should be different between baseline and verify.
    assert verify_report.sbom_digest != baseline_report.sbom_digest


def test_drift_not_flipped_when_sbom_unchanged() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    runner.register("syft", stdout=json.dumps(_SYFT_BASELINE))
    config = SecurityScanConfig(enable_trivy=True, enable_syft=True)
    agent = ClaudeSecurityAgent(runner=runner, config=config)

    req = SecurityRequest(
        kind="baseline",
        experiment_id="exp-cccccccccccc",
        target_app="otel-demo",
        target_images=["x:latest"],
    )
    asyncio.run(agent.baseline(req))
    verify_report = asyncio.run(agent.verify(
        SecurityRequest(
            kind="verify",
            experiment_id=req.experiment_id,
            target_app="otel-demo",
            target_images=["x:latest"],
        )
    ))
    assert verify_report.sbom_drift_from_baseline is False


def test_gitleaks_only_runs_when_target_repo_set() -> None:
    """gitleaks needs a repo path; with none, it must not fire."""
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    runner.register("gitleaks", stdout="[]")
    config = SecurityScanConfig(enable_trivy=True, enable_gitleaks=True)
    agent = ClaudeSecurityAgent(runner=runner, config=config)

    # No target_repo on the request -> gitleaks must not be invoked.
    req_no_repo = SecurityRequest(
        kind="baseline",
        experiment_id="exp-dddddddddddd",
        target_app="otel-demo",
        target_images=["x:latest"],
    )
    asyncio.run(agent.baseline(req_no_repo))
    assert not any(c[0] == "gitleaks" for c in runner.calls)

    # With target_repo set, gitleaks fires.
    req_with_repo = SecurityRequest(
        kind="baseline",
        experiment_id="exp-eeeeeeeeeeee",
        target_app="otel-demo",
        target_repo="/path/to/repo",
        target_images=["x:latest"],
    )
    asyncio.run(agent.baseline(req_with_repo))
    assert any(c[0] == "gitleaks" for c in runner.calls)


def test_cosign_runs_when_enabled_with_key() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    runner.register("cosign", returncode=0)
    config = SecurityScanConfig(
        enable_trivy=True,
        enable_cosign=True,
        cosign_public_key="/keys/pub.pem",
    )
    agent = ClaudeSecurityAgent(runner=runner, config=config)
    asyncio.run(agent.baseline(_req("x:latest")))
    assert any(c[0] == "cosign" for c in runner.calls)


def test_kubescape_runs_when_enabled() -> None:
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_NO_VULNS))
    runner.register("kubescape", stdout=json.dumps({"summaryDetails": {"controls": {}}}))
    config = SecurityScanConfig(enable_trivy=True, enable_kubescape=True)
    agent = ClaudeSecurityAgent(runner=runner, config=config)
    asyncio.run(agent.baseline(_req("x:latest")))
    assert any(c[0] == "kubescape" for c in runner.calls)


def test_skip_on_scanner_error_isolates_failed_scanner() -> None:
    """A failing Grype must not prevent other scanners' results from landing."""
    runner = FixtureRunner()
    runner.register("trivy", stdout=json.dumps(_TRIVY_TWO_VULNS))
    runner.register("grype", stdout="garbage-not-json")  # parse error -> ScannerError
    config = SecurityScanConfig(
        enable_trivy=True,
        enable_grype=True,
    )
    agent = ClaudeSecurityAgent(runner=runner, config=config, skip_on_scanner_error=True)
    report = asyncio.run(agent.baseline(_req("x:latest")))
    # Trivy's 2 findings survive; grype contributed 0.
    assert len(report.findings) == 2
    assert all(f.scanner == "trivy" for f in report.findings)
