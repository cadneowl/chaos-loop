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
