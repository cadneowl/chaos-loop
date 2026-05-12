"""Tests for the tester's hypothesize mode and the parser.

Notes:
    - We never invoke a real LLM in CI. Tests use FixtureHypothesizer.
    - The JSON parser is tested directly with model-output-shaped strings.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.tester.agent import ClaudeTesterAgent
from agents.tester.hypothesizer import (
    ClaudeHypothesizer,
    FixtureHypothesizer,
    _parse_hypotheses,
)
from shared.contracts import Hypothesis, TesterRequest

# ---------------------------------------------------------------------------- #
# Parser                                                                       #
# ---------------------------------------------------------------------------- #


_GOOD_HYPOTHESIS_JSON = """
[
  {
    "id": "h-otel-cart-redis-001",
    "statement": "cartservice must degrade gracefully when valkey-cart is unreachable",
    "rationale": "no fallback path is visible in services/cart/handler.py:42",
    "proposed_fault": "network.loss",
    "success_criteria": ["GET /api/cart returns 503 within 2s"],
    "confidence": 0.85,
    "code_references": ["services/cart/handler.py:42"]
  }
]
"""


def test_parse_returns_validated_hypotheses() -> None:
    out = _parse_hypotheses(_GOOD_HYPOTHESIS_JSON)
    assert len(out) == 1
    assert out[0].id == "h-otel-cart-redis-001"
    assert out[0].proposed_fault == "network.loss"
    assert out[0].confidence == 0.85


def test_parse_strips_code_fences() -> None:
    fenced = f"Sure, here are the hypotheses:\n```json\n{_GOOD_HYPOTHESIS_JSON}\n```\nDone!"
    out = _parse_hypotheses(fenced)
    assert len(out) == 1


def test_parse_handles_object_form() -> None:
    """Some models return a single object instead of a one-element array."""
    obj = """
    {
      "id": "h-1",
      "statement": "x must y",
      "rationale": "because z",
      "proposed_fault": "pod.kill",
      "success_criteria": ["something"],
      "confidence": 0.5
    }
    """
    out = _parse_hypotheses(obj)
    assert len(out) == 1
    assert out[0].id == "h-1"


def test_parse_drops_invalid_items_keeps_valid() -> None:
    mixed = """[
        {"id": "h-good", "statement": "x must y", "rationale": "r",
         "proposed_fault": "pod.kill", "success_criteria": ["c"], "confidence": 0.7},
        {"id": "h-bad-missing-fields"},
        "totally not a hypothesis"
    ]"""
    out = _parse_hypotheses(mixed)
    assert len(out) == 1
    assert out[0].id == "h-good"


def test_parse_handles_empty_input() -> None:
    assert _parse_hypotheses("") == []
    assert _parse_hypotheses("   \n  ") == []


def test_parse_handles_non_json() -> None:
    assert _parse_hypotheses("I can't help with that.") == []


# ---------------------------------------------------------------------------- #
# Fixture hypothesizer                                                         #
# ---------------------------------------------------------------------------- #


def _hyp(
    name: str = "h-test-001",
    proposed_fault: str = "network.loss",
    confidence: float = 0.8,
) -> Hypothesis:
    return Hypothesis(
        id=name,
        statement=f"target must tolerate {proposed_fault}",
        rationale="fixture reasoning",
        proposed_fault=proposed_fault,
        success_criteria=["criterion"],
        confidence=confidence,
        code_references=["src/x.py:1"],
    )


def _req(target_app: str = "otel-demo") -> TesterRequest:
    return TesterRequest(
        kind="hypothesize",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app=target_app,
    )


def test_hypothesize_returns_report_with_hypotheses() -> None:
    fixture = FixtureHypothesizer([_hyp(), _hyp("h-test-002", "pod.kill")])
    agent = ClaudeTesterAgent(hypothesizer=fixture)
    report = asyncio.run(agent.hypothesize(_req()))

    assert report.request_kind == "hypothesize"
    assert len(report.generated_hypotheses) == 2
    assert report.steady_state is True  # hypothesize is not a steady-state mode
    assert "2 accepted" in report.notes


def test_hypothesize_rejects_hypotheses_with_unknown_fault() -> None:
    """Hallucinated fault names must NOT propagate to the chaos agent."""
    fixture = FixtureHypothesizer([
        _hyp("h-good", "network.loss"),
        _hyp("h-bad", "totally.made.up.fault"),
    ])
    agent = ClaudeTesterAgent(hypothesizer=fixture)
    report = asyncio.run(agent.hypothesize(_req()))

    assert len(report.generated_hypotheses) == 1
    assert report.generated_hypotheses[0].id == "h-good"
    # The rejection is recorded as an anomaly so reviewers can see what happened.
    assert any("h-bad" in a for a in report.anomalies)


def test_hypothesize_handles_hypothesizer_failure() -> None:
    """If the hypothesizer raises, the report surfaces the error, not a crash."""

    async def boom(*_a, **_kw):
        raise RuntimeError("LLM call failed")

    fixture = FixtureHypothesizer(boom)
    agent = ClaudeTesterAgent(hypothesizer=fixture)
    report = asyncio.run(agent.hypothesize(_req()))

    assert report.generated_hypotheses == []
    assert report.steady_state is False
    assert any("LLM call failed" in a for a in report.anomalies)


def test_hypothesize_passes_target_to_hypothesizer() -> None:
    captured: dict = {}

    async def capture(target_app, target_repo, code) -> list[Hypothesis]:
        captured["target_app"] = target_app
        captured["target_repo"] = target_repo
        return [_hyp()]

    fixture = FixtureHypothesizer(capture)
    agent = ClaudeTesterAgent(hypothesizer=fixture)
    req = TesterRequest(
        kind="hypothesize",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app="my-app",
        target_repo="https://example.invalid/repo.git",
    )
    asyncio.run(agent.hypothesize(req))
    assert captured["target_app"] == "my-app"
    assert captured["target_repo"] == "https://example.invalid/repo.git"


# ---------------------------------------------------------------------------- #
# ClaudeHypothesizer guardrails                                                #
# ---------------------------------------------------------------------------- #


def test_claude_hypothesizer_returns_empty_without_code_reader(caplog) -> None:
    """Without a code reader the LLM has nothing to read; degrade silently to []
    (mirrors StaticHypothesizer's behavior so the Hypothesizer Protocol is
    consistent across implementations). Emits a warning so it's not invisible."""
    import logging
    h = ClaudeHypothesizer()
    with caplog.at_level(logging.WARNING, logger="agents.tester.hypothesizer"):
        result = asyncio.run(h.generate(target_app="x", target_repo=None, code=None))
    assert result == []
    assert any("TargetCodeReader" in rec.message for rec in caplog.records)


def test_claude_hypothesizer_default_model() -> None:
    h = ClaudeHypothesizer()
    assert h.model == "claude-opus-4-7"
    assert h.max_budget_usd > 0
    assert h.max_turns > 0


def test_claude_hypothesizer_custom_config() -> None:
    h = ClaudeHypothesizer(model="claude-haiku-4-5-20251001", max_turns=10, max_budget_usd=0.5)
    assert h.model == "claude-haiku-4-5-20251001"
    assert h.max_turns == 10
    assert h.max_budget_usd == 0.5


# ---------------------------------------------------------------------------- #
# Empty input edge case                                                        #
# ---------------------------------------------------------------------------- #


def test_hypothesize_empty_hypothesizer_output(tmp_path: Path) -> None:
    """Hypothesizer returns nothing -> empty report (still valid)."""
    agent = ClaudeTesterAgent(hypothesizer=FixtureHypothesizer([]))
    report = asyncio.run(agent.hypothesize(_req()))
    assert report.generated_hypotheses == []
    assert "0 accepted" in report.notes
