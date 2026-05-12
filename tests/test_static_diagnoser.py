"""Tests for StaticDiagnoser — rule-based fault → fix-class mapping."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agents.diagnostician.diagnoser import StaticDiagnoser
from shared.contracts import (
    ChaosTimeline,
    DiagnosisRequest,
    SecurityFinding,
    SecurityReport,
    TesterReport,
    TimelineEvent,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _timeline(*fault_names: str) -> ChaosTimeline:
    """Build a minimal timeline with one started event per fault."""
    ts = datetime.now(tz=UTC)
    events = []
    for name in fault_names:
        events.append(TimelineEvent(timestamp=ts, fault_name=name, event="started"))
        events.append(TimelineEvent(timestamp=ts, fault_name=name, event="cleaned-up"))
    return ChaosTimeline(experiment_id="exp-aaaaaaaaaaaa", events=events, success=True)


def _failed_tester(
    failed_probes: list[str] | None = None,
    anomalies: list[str] | None = None,
    notes: str = "",
) -> TesterReport:
    return TesterReport(
        request_kind="verify",
        experiment_id="exp-aaaaaaaaaaaa",
        steady_state=False,
        failed_probes=failed_probes or [],
        anomalies=anomalies or [],
        notes=notes,
    )


def _request(
    timeline: ChaosTimeline,
    *,
    tester: TesterReport | None = None,
    security: SecurityReport | None = None,
) -> DiagnosisRequest:
    if tester is None and security is None:
        tester = _failed_tester(failed_probes=["dummy"])
    return DiagnosisRequest(
        experiment_id="exp-aaaaaaaaaaaa",
        failed_tester_report=tester,
        failed_security_report=security,
        chaos_timeline=timeline,
    )


def _diagnose(req: DiagnosisRequest) -> list:
    return asyncio.run(StaticDiagnoser().diagnose(request=req))


# --------------------------------------------------------------------------- #
# Per-category routing                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fault_name,expected_top_class",
    [
        ("network.loss", "missing-retry"),
        ("network.delay", "missing-retry"),
        ("pod.kill", "missing-fallback"),
        ("stress.cpu", "missing-timeout"),
        ("cert.revoke", "secret-handling"),
        ("tls.downgrade", "auth-control-gap"),
        ("auth.outage", "auth-control-gap"),
        ("secret.rotate", "secret-handling"),
        ("image.swap_vuln", "image-policy"),
        ("iam.degrade", "auth-control-gap"),
        ("netpol.regress", "config-change"),
        ("dns.error", "missing-retry"),
        ("io.latency", "missing-timeout"),
        ("http.abort", "missing-retry"),
    ],
)
def test_each_fault_category_routes_to_expected_fix_class(
    fault_name: str, expected_top_class: str
) -> None:
    """Without symptom boosts, the highest-confidence rule per category wins."""
    hyps = _diagnose(_request(_timeline(fault_name)))
    assert hyps, f"no hypotheses for {fault_name}"
    assert hyps[0].suggested_fix_class == expected_top_class


# --------------------------------------------------------------------------- #
# Symptom-driven confidence boosts                                            #
# --------------------------------------------------------------------------- #


def test_latency_anomaly_boosts_missing_timeout() -> None:
    """A 'latency' / 'p95' / 'hung' mention should boost missing-timeout
    enough to overtake missing-retry as the top candidate."""
    tester = _failed_tester(
        failed_probes=["cart_p95_latency_ms"],
        anomalies=["cart_p95_latency_ms: value 5000 >= threshold 500 (call hung)"],
    )
    hyps = _diagnose(_request(_timeline("network.loss"), tester=tester))
    # The boost (latency + p95 + hung = +0.45) should push missing-timeout above
    # the base 0.55 of missing-retry.
    assert hyps[0].suggested_fix_class == "missing-timeout"


def test_5xx_anomaly_does_not_overtake_missing_retry() -> None:
    """A modest boost to missing-retry should not overtake itself; sanity."""
    tester = _failed_tester(anomalies=["error rate spiked: 5xx from cart-service"])
    hyps = _diagnose(_request(_timeline("network.loss"), tester=tester))
    assert hyps[0].suggested_fix_class == "missing-retry"


def test_security_finding_appears_in_evidence() -> None:
    sec = SecurityReport(
        request_kind="verify",
        experiment_id="exp-aaaaaaaaaaaa",
        findings=[
            SecurityFinding(
                id="f-1",
                severity="critical",  # type: ignore[arg-type]
                title="CVE-2024-9999 in libfoo",
                description="rce",
                scanner="grype",
            )
        ],
    )
    hyps = _diagnose(_request(_timeline("image.swap_vuln"), security=sec))
    assert hyps[0].suggested_fix_class == "image-policy"
    assert any("CVE-2024-9999" in e for e in hyps[0].evidence)


# --------------------------------------------------------------------------- #
# Edge cases                                                                  #
# --------------------------------------------------------------------------- #


def test_empty_timeline_yields_working_as_intended_floor() -> None:
    """No catalogued fault in the timeline -> static rules can't say much."""
    hyps = _diagnose(_request(_timeline()))
    assert len(hyps) == 1
    assert hyps[0].suggested_fix_class == "working-as-intended"
    assert hyps[0].confidence < 0.2


def test_fault_not_in_catalogue_is_skipped() -> None:
    """Synthetic events like '(preflight)' aren't faults; static rules ignore them."""
    timeline = ChaosTimeline(
        experiment_id="exp-aaaaaaaaaaaa",
        events=[
            TimelineEvent(
                timestamp=datetime.now(tz=UTC),
                fault_name="(preflight)",
                event="error",
                detail="some preflight failure",
            )
        ],
        success=False,
    )
    hyps = _diagnose(_request(timeline))
    # Falls through to the working-as-intended floor.
    assert hyps[0].suggested_fix_class == "working-as-intended"


def test_multi_fault_timeline_aggregates() -> None:
    """Multiple distinct faults -> hypotheses span both fault categories."""
    hyps = _diagnose(_request(_timeline("network.loss", "auth.outage")))
    fix_classes = {h.suggested_fix_class for h in hyps}
    assert "missing-retry" in fix_classes
    assert "auth-control-gap" in fix_classes


def test_max_hypotheses_caps_output() -> None:
    """Default cap of 5; explicit cap respected."""
    diagnoser = StaticDiagnoser(max_hypotheses=2)
    hyps = asyncio.run(
        diagnoser.diagnose(
            request=_request(_timeline("network.loss", "auth.outage", "image.swap_vuln"))
        )
    )
    assert len(hyps) <= 2


def test_hypotheses_ranked_by_confidence_descending() -> None:
    hyps = _diagnose(_request(_timeline("network.loss")))
    confs = [h.confidence for h in hyps]
    assert confs == sorted(confs, reverse=True)


def test_evidence_cites_timeline_event() -> None:
    hyps = _diagnose(_request(_timeline("network.loss")))
    # Every hypothesis should reference the timeline event as the first piece of evidence.
    assert any("network.loss" in e for e in hyps[0].evidence)


def test_repeated_fault_in_timeline_only_counted_once() -> None:
    """A scheduled+started+cleaned-up trio shouldn't triple-count the fault."""
    ts = datetime.now(tz=UTC)
    timeline = ChaosTimeline(
        experiment_id="exp-aaaaaaaaaaaa",
        events=[
            TimelineEvent(timestamp=ts, fault_name="network.loss", event="scheduled"),
            TimelineEvent(timestamp=ts, fault_name="network.loss", event="started"),
            TimelineEvent(timestamp=ts, fault_name="network.loss", event="cleaned-up"),
        ],
        success=True,
    )
    hyps = _diagnose(_request(timeline))
    # The category has 3 candidate fix-classes; we should see those once,
    # not 3x3 = 9.
    assert len(hyps) <= 3
