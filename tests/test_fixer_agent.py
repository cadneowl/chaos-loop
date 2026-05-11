"""Tests for ClaudeFixerAgent.propose_fix() against FixtureFixerStrategy."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.fixer.agent import ClaudeFixerAgent
from agents.fixer.strategy import (
    ClaudeFixerStrategy,
    FixerOutput,
    FixtureFixerStrategy,
)
from shared.contracts import (
    DiagnosisReport,
    FixAction,
    RootCauseHypothesis,
)

# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _hyp(
    fix_class: str = "missing-fallback",
    confidence: float = 0.8,
    summary: str = "cart hard-deps redis",
    paths: list[str] | None = None,
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=confidence,
        evidence=["log line at services/cart/handler.py:42"],
        suggested_fix_class=fix_class,  # type: ignore[arg-type]
        affected_paths=paths or ["services/cart/handler.py"],
    )


def _diagnosis(*hyps: RootCauseHypothesis) -> DiagnosisReport:
    return DiagnosisReport(
        experiment_id="exp-aaaaaaaaaaaa",
        hypotheses=list(hyps),
    )


def _run(agent: ClaudeFixerAgent, diagnosis: DiagnosisReport):
    return asyncio.run(agent.propose_fix(diagnosis))


# ---------------------------------------------------------------------------- #
# action=NONE paths                                                            #
# ---------------------------------------------------------------------------- #


def test_low_confidence_yields_none() -> None:
    agent = ClaudeFixerAgent()
    proposal = _run(agent, _diagnosis(_hyp(confidence=0.3)))
    assert proposal.action == FixAction.NONE
    assert "below" in proposal.reasoning or "<" in proposal.reasoning
    assert proposal.is_draft is True
    assert proposal.files_touched == []


def test_code_patch_without_strategy_yields_none() -> None:
    """If the agent has no strategy configured, code/config actions fail gracefully."""
    agent = ClaudeFixerAgent(strategy=None)
    proposal = _run(agent, _diagnosis(_hyp(confidence=0.9, fix_class="missing-retry")))
    assert proposal.action == FixAction.NONE
    assert "no FixerStrategy" in proposal.reasoning


def test_denylisted_path_yields_none(tmp_path: Path) -> None:
    """Strategy can't push us past the denylist."""
    strategy = FixtureFixerStrategy(
        FixerOutput(
            reasoning="patched the workflow",
            files_touched=[".github/workflows/release.yml"],
        )
    )
    agent = ClaudeFixerAgent(strategy=strategy, runs_dir=tmp_path)
    proposal = _run(agent, _diagnosis(_hyp(confidence=0.9, fix_class="config-change")))
    assert proposal.action == FixAction.NONE
    assert "denylisted" in proposal.reasoning
    assert ".github/workflows/release.yml" in proposal.reasoning


# ---------------------------------------------------------------------------- #
# action=DOC_ONLY (working-as-intended)                                        #
# ---------------------------------------------------------------------------- #


def test_working_as_intended_writes_doc(tmp_path: Path) -> None:
    agent = ClaudeFixerAgent(runs_dir=tmp_path)
    diagnosis = _diagnosis(
        _hyp(
            fix_class="working-as-intended",
            confidence=0.9,
            summary="cart depends on redis as documented; chaos hit a real but expected dependency",
        )
    )
    proposal = _run(agent, diagnosis)
    assert proposal.action == FixAction.DOC_ONLY
    assert proposal.pr_url is None
    assert proposal.is_draft is True
    # File should have been created.
    doc_path = tmp_path / "exp-aaaaaaaaaaaa" / "proposed" / "working-as-intended.md"
    assert doc_path.exists()
    body = doc_path.read_text(encoding="utf-8")
    assert "working-as-intended" in body
    assert "cart depends on redis" in body
    assert "exp-aaaaaaaaaaaa" in body


def test_working_as_intended_low_confidence_still_yields_none() -> None:
    """Confidence gate runs BEFORE action routing."""
    agent = ClaudeFixerAgent()
    proposal = _run(
        agent,
        _diagnosis(_hyp(fix_class="working-as-intended", confidence=0.3)),
    )
    assert proposal.action == FixAction.NONE


# ---------------------------------------------------------------------------- #
# action=CODE_PATCH / CONFIG_CHANGE happy paths                                #
# ---------------------------------------------------------------------------- #


def test_code_patch_via_strategy(tmp_path: Path) -> None:
    strategy = FixtureFixerStrategy(
        FixerOutput(
            reasoning="added 3-retry exponential backoff around the redis.Get call",
            files_touched=[
                "services/cart/redis_client.py",
                "services/cart/tests/test_redis_retry.py",
            ],
            regression_test_added=True,
            pr_url="https://example.invalid/pr/42",
        )
    )
    agent = ClaudeFixerAgent(strategy=strategy, runs_dir=tmp_path)
    proposal = _run(
        agent, _diagnosis(_hyp(fix_class="missing-retry", confidence=0.85))
    )
    assert proposal.action == FixAction.CODE_PATCH
    assert proposal.pr_url == "https://example.invalid/pr/42"
    assert proposal.regression_test_added
    assert "services/cart/redis_client.py" in proposal.files_touched
    assert proposal.is_draft is True


def test_config_change_via_strategy(tmp_path: Path) -> None:
    strategy = FixtureFixerStrategy(
        FixerOutput(
            reasoning="updated Kyverno policy to require cosign signatures",
            files_touched=["policy/require-signed-images.yaml"],
            regression_test_added=False,
        )
    )
    agent = ClaudeFixerAgent(strategy=strategy, runs_dir=tmp_path)
    proposal = _run(
        agent, _diagnosis(_hyp(fix_class="image-policy", confidence=0.85))
    )
    assert proposal.action == FixAction.CONFIG_CHANGE
    assert proposal.files_touched == ["policy/require-signed-images.yaml"]


def test_strategy_receives_diagnosis_and_action() -> None:
    captured: dict = {}

    async def capture(diagnosis: DiagnosisReport, action: FixAction) -> FixerOutput:
        captured["diagnosis"] = diagnosis
        captured["action"] = action
        return FixerOutput(reasoning="ok", files_touched=["src/x.py"])

    strategy = FixtureFixerStrategy(capture)
    agent = ClaudeFixerAgent(strategy=strategy)
    _run(agent, _diagnosis(_hyp(fix_class="missing-retry", confidence=0.9)))
    assert captured["action"] == FixAction.CODE_PATCH
    assert captured["diagnosis"].experiment_id == "exp-aaaaaaaaaaaa"


# ---------------------------------------------------------------------------- #
# Schema invariants                                                            #
# ---------------------------------------------------------------------------- #


def test_proposal_is_always_draft(tmp_path: Path) -> None:
    """is_draft=True is enforced by the contract; tests across all action paths."""
    agent_none = ClaudeFixerAgent()
    assert _run(agent_none, _diagnosis(_hyp(confidence=0.1))).is_draft is True

    agent_doc = ClaudeFixerAgent(runs_dir=tmp_path)
    diag = _diagnosis(_hyp(fix_class="working-as-intended", confidence=0.9))
    assert _run(agent_doc, diag).is_draft is True

    strategy = FixtureFixerStrategy(
        FixerOutput(reasoning="r", files_touched=["src/a.py"], regression_test_added=True)
    )
    agent_code = ClaudeFixerAgent(strategy=strategy)
    diag = _diagnosis(_hyp(fix_class="missing-retry", confidence=0.9))
    assert _run(agent_code, diag).is_draft is True


def test_claude_strategy_stub_raises() -> None:
    strategy = ClaudeFixerStrategy()
    with pytest.raises(NotImplementedError):
        asyncio.run(
            strategy.propose(
                diagnosis=_diagnosis(_hyp()),
                intended_action=FixAction.CODE_PATCH,
            )
        )
