"""Tests for ClaudeDiagnosticianAgent against FixtureDiagnoser."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agents.diagnostician.agent import ClaudeDiagnosticianAgent
from agents.diagnostician.diagnoser import ClaudeDiagnoser, FixtureDiagnoser
from shared.contracts import (
    ChaosTimeline,
    DiagnosisRequest,
    RootCauseHypothesis,
    TesterReport,
    TimelineEvent,
)

# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _timeline(experiment_id: str = "exp-aaaaaaaaaaaa") -> ChaosTimeline:
    ts = datetime.now(tz=UTC)
    return ChaosTimeline(
        experiment_id=experiment_id,
        events=[
            TimelineEvent(timestamp=ts, fault_name="network.loss", event="started"),
            TimelineEvent(timestamp=ts, fault_name="network.loss", event="cleaned-up"),
        ],
        success=True,
    )


def _failed_tester(experiment_id: str = "exp-aaaaaaaaaaaa") -> TesterReport:
    return TesterReport(
        request_kind="verify",
        experiment_id=experiment_id,
        steady_state=False,
        failed_probes=["cart_p95_latency_ms"],
        anomalies=["cart_p95_latency_ms: value 5000 >= threshold 500"],
    )


def _req() -> DiagnosisRequest:
    return DiagnosisRequest(
        experiment_id="exp-aaaaaaaaaaaa",
        failed_tester_report=_failed_tester(),
        chaos_timeline=_timeline(),
    )


def _h(summary: str, conf: float, *, fix: str = "missing-retry") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=conf,
        evidence=["mock evidence"],
        suggested_fix_class=fix,  # type: ignore[arg-type]
        affected_paths=["src/cart.py"],
    )


# ---------------------------------------------------------------------------- #
# Happy paths                                                                  #
# ---------------------------------------------------------------------------- #


def test_returns_diagnosis_report() -> None:
    diagnoser = FixtureDiagnoser([_h("cart hard-deps redis", 0.8)])
    agent = ClaudeDiagnosticianAgent(diagnoser=diagnoser)
    report = asyncio.run(agent.diagnose(_req()))
    assert report.experiment_id == "exp-aaaaaaaaaaaa"
    assert len(report.hypotheses) == 1
    assert report.hypotheses[0].summary == "cart hard-deps redis"
    # notes summarize the input.
    assert "tester" in report.notes
    assert "chaos" in report.notes


def test_hypotheses_ranked_by_confidence_desc() -> None:
    diagnoser = FixtureDiagnoser([
        _h("lowprio", 0.3),
        _h("topprio", 0.9),
        _h("midprio", 0.6),
    ])
    agent = ClaudeDiagnosticianAgent(diagnoser=diagnoser)
    report = asyncio.run(agent.diagnose(_req()))
    confs = [h.confidence for h in report.hypotheses]
    assert confs == sorted(confs, reverse=True)
    assert report.hypotheses[0].summary == "topprio"


def test_empty_diagnoser_output_yields_working_as_intended_hypothesis() -> None:
    """DiagnosisReport requires >=1 hypothesis. If the diagnoser produces none,
    the agent emits a low-confidence working-as-intended placeholder so the
    contract holds and the fixer can choose action=NONE."""
    diagnoser = FixtureDiagnoser([])
    agent = ClaudeDiagnosticianAgent(diagnoser=diagnoser)
    report = asyncio.run(agent.diagnose(_req()))
    assert len(report.hypotheses) == 1
    assert report.hypotheses[0].suggested_fix_class == "working-as-intended"
    assert report.hypotheses[0].confidence == 0.0


def test_diagnoser_receives_request() -> None:
    """Verify the diagnoser actually sees the DiagnosisRequest we passed in."""
    captured: dict = {}

    async def capture(request: DiagnosisRequest) -> list[RootCauseHypothesis]:
        captured["request"] = request
        return [_h("ok", 0.5)]

    diagnoser = FixtureDiagnoser(capture)
    agent = ClaudeDiagnosticianAgent(diagnoser=diagnoser)
    asyncio.run(agent.diagnose(_req()))
    assert captured["request"].experiment_id == "exp-aaaaaaaaaaaa"
    assert captured["request"].failed_tester_report is not None
    assert captured["request"].failed_tester_report.failed_probes == ["cart_p95_latency_ms"]


# ---------------------------------------------------------------------------- #
# Schema invariants                                                            #
# ---------------------------------------------------------------------------- #


def test_diagnosis_request_requires_a_failed_report() -> None:
    """Pydantic enforces the at-least-one-failure rule on the request."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="at least one failed report"):
        DiagnosisRequest(
            experiment_id="exp-aaaaaaaaaaaa",
            chaos_timeline=_timeline(),
        )


def test_claude_diagnoser_is_constructible_without_invoking() -> None:
    """M5.x: ClaudeDiagnoser is real. Constructing it must NOT call the LLM —
    tests + dry-run wire it up cheaply and only pay tokens at diagnose() time."""
    diagnoser = ClaudeDiagnoser()
    assert diagnoser.model == "claude-opus-4-7"
    # The actual diagnose() call requires the claude CLI + API; not exercised here.
