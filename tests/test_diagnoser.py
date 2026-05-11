"""Tests for the diagnoser strategies — fixtures + the JSON parser used by ClaudeDiagnoser."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from agents.diagnostician.diagnoser import (
    ClaudeDiagnoser,
    FixtureDiagnoser,
    _build_user_prompt,
    _chaos_window,
    _parse_hypotheses,
)
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


def _timeline() -> ChaosTimeline:
    ts = datetime.now(tz=UTC)
    return ChaosTimeline(
        experiment_id="exp-aaaaaaaaaaaa",
        events=[
            TimelineEvent(timestamp=ts, fault_name="network.loss", event="started"),
            TimelineEvent(timestamp=ts, fault_name="network.loss", event="cleaned-up"),
        ],
        success=True,
    )


def _failed_tester() -> TesterReport:
    return TesterReport(
        request_kind="verify",
        experiment_id="exp-aaaaaaaaaaaa",
        steady_state=False,
        failed_probes=["cart_p95_latency_ms"],
        anomalies=["cart_p95_latency_ms: value 5000 >= threshold 500"],
    )


def _req() -> DiagnosisRequest:
    return DiagnosisRequest(
        experiment_id="exp-aaaaaaaaaaaa",
        failed_tester_report=_failed_tester(),
        chaos_timeline=_timeline(),
        target_repo="https://example.invalid/repo.git",
    )


def _hyp(summary: str = "cart hard-deps redis", confidence: float = 0.8) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=confidence,
        evidence=["fixture evidence"],
        suggested_fix_class="missing-retry",
        affected_paths=["src/cart.py"],
    )


# ---------------------------------------------------------------------------- #
# FixtureDiagnoser (covered elsewhere; sanity here)                            #
# ---------------------------------------------------------------------------- #


def test_fixture_diagnoser_static_list() -> None:
    fd = FixtureDiagnoser([_hyp("a", 0.9), _hyp("b", 0.6)])
    out = asyncio.run(fd.diagnose(request=_req()))
    assert [h.summary for h in out] == ["a", "b"]


def test_fixture_diagnoser_callback() -> None:
    async def cb(req: DiagnosisRequest) -> list[RootCauseHypothesis]:
        return [_hyp(req.experiment_id)]

    fd = FixtureDiagnoser(cb)
    out = asyncio.run(fd.diagnose(request=_req()))
    assert out[0].summary == "exp-aaaaaaaaaaaa"


# ---------------------------------------------------------------------------- #
# JSON parser                                                                  #
# ---------------------------------------------------------------------------- #


_GOOD_RCH_JSON = """
[
  {
    "summary": "cart depends on redis with no retry",
    "confidence": 0.85,
    "evidence": ["services/cart/handler.py:42", "loki: 12345: connection refused"],
    "suggested_fix_class": "missing-retry",
    "affected_paths": ["services/cart/handler.py"]
  }
]
"""


def test_parse_valid_hypothesis() -> None:
    out = _parse_hypotheses(_GOOD_RCH_JSON)
    assert len(out) == 1
    assert out[0].suggested_fix_class == "missing-retry"
    assert out[0].confidence == 0.85


def test_parse_strips_code_fence() -> None:
    fenced = f"Here's my diagnosis:\n```json\n{_GOOD_RCH_JSON}\n```\nLet me know."
    out = _parse_hypotheses(fenced)
    assert len(out) == 1


def test_parse_drops_invalid_fix_class() -> None:
    """Hallucinated fix_class values are dropped, not coerced."""
    payload = """[
        {"summary": "a", "confidence": 0.9, "evidence": ["x"],
         "suggested_fix_class": "missing-retry", "affected_paths": []},
        {"summary": "b", "confidence": 0.9, "evidence": ["x"],
         "suggested_fix_class": "totally-invented-class", "affected_paths": []}
    ]"""
    out = _parse_hypotheses(payload)
    assert len(out) == 1
    assert out[0].summary == "a"


def test_parse_handles_object_form() -> None:
    payload = """{
        "summary": "single hypothesis",
        "confidence": 0.7,
        "evidence": ["x"],
        "suggested_fix_class": "config-change",
        "affected_paths": []
    }"""
    out = _parse_hypotheses(payload)
    assert len(out) == 1
    assert out[0].suggested_fix_class == "config-change"


def test_parse_drops_invalid_items_keeps_valid() -> None:
    payload = """[
        {"summary": "good", "confidence": 0.7, "evidence": ["e"],
         "suggested_fix_class": "missing-retry", "affected_paths": []},
        {"summary": "missing confidence", "suggested_fix_class": "missing-retry"},
        "not even a dict"
    ]"""
    out = _parse_hypotheses(payload)
    assert len(out) == 1
    assert out[0].summary == "good"


def test_parse_empty_or_garbage() -> None:
    assert _parse_hypotheses("") == []
    assert _parse_hypotheses("just prose, no JSON") == []
    assert _parse_hypotheses("[unclosed") == []


def test_parse_accepts_all_valid_fix_classes() -> None:
    """Every Literal value in suggested_fix_class should parse through."""
    fix_classes = [
        "code-patch",
        "config-change",
        "missing-retry",
        "missing-timeout",
        "missing-circuit-breaker",
        "missing-fallback",
        "auth-control-gap",
        "secret-handling",
        "image-policy",
        "test-gap",
        "working-as-intended",
    ]
    payload = json.dumps(
        [
            {
                "summary": f"hyp {i}",
                "confidence": 0.5,
                "evidence": ["e"],
                "suggested_fix_class": fc,
                "affected_paths": [],
            }
            for i, fc in enumerate(fix_classes)
        ]
    )
    out = _parse_hypotheses(payload)
    assert len(out) == len(fix_classes)


# ---------------------------------------------------------------------------- #
# Window + prompt builders                                                     #
# ---------------------------------------------------------------------------- #


def test_chaos_window_brackets_timeline() -> None:
    req = _req()
    start, end = _chaos_window(req)
    first_ts = req.chaos_timeline.events[0].timestamp.timestamp()
    last_ts = req.chaos_timeline.events[-1].timestamp.timestamp()
    # 30s pre buffer, 60s post.
    assert start == pytest.approx(first_ts - 30.0)
    assert end == pytest.approx(last_ts + 60.0)


def test_user_prompt_includes_window_and_failures() -> None:
    req = _req()
    start, end = _chaos_window(req)
    prompt = _build_user_prompt(req, start, end)
    # Window timestamps are present.
    assert str(start) in prompt or f"{start:.6f}" in prompt or json.dumps(start) in prompt
    # Failed tester report is encoded in the JSON payload.
    assert "cart_p95_latency_ms" in prompt
    # Chaos timeline events too.
    assert "network.loss" in prompt
    # Instruction to return JSON.
    assert "JSON" in prompt


# ---------------------------------------------------------------------------- #
# ClaudeDiagnoser guardrails                                                   #
# ---------------------------------------------------------------------------- #


def test_claude_diagnoser_defaults() -> None:
    cd = ClaudeDiagnoser()
    assert cd.model == "claude-opus-4-7"
    assert cd.max_budget_usd > 0
    assert cd.max_turns > 0


def test_claude_diagnoser_custom() -> None:
    cd = ClaudeDiagnoser(model="claude-haiku-4-5-20251001", max_turns=10, max_budget_usd=0.5)
    assert cd.model == "claude-haiku-4-5-20251001"
    assert cd.max_turns == 10
    assert cd.max_budget_usd == 0.5
