"""Tests for ClaudeFixerStrategy parser + artifact persistence + guardrails."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.fixer.strategy import (
    ClaudeFixerStrategy,
    FixerOutput,
    _build_user_prompt,
    _parse_fix_proposal,
)
from shared.contracts import DiagnosisReport, FixAction, RootCauseHypothesis


def _hyp(
    summary: str = "redis hard dep",
    confidence: float = 0.85,
    fix_class: str = "missing-retry",
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=confidence,
        evidence=["fixture evidence"],
        suggested_fix_class=fix_class,  # type: ignore[arg-type]
        affected_paths=["services/cart/redis_client.py"],
    )


def _diagnosis() -> DiagnosisReport:
    return DiagnosisReport(
        experiment_id="exp-aaaaaaaaaaaa",
        hypotheses=[_hyp()],
    )


# ---------------------------------------------------------------------------- #
# Parser                                                                       #
# ---------------------------------------------------------------------------- #


_GOOD_FIX_JSON = """
{
  "reasoning": "Added a 3-retry exponential backoff around the redis.Get call.",
  "files_touched": ["services/cart/redis_client.py", "services/cart/tests/test_retry.py"],
  "regression_test_added": true,
  "edits": [
    {"path": "services/cart/redis_client.py", "intent": "wrap redis.Get in retry loop"},
    {"path": "services/cart/tests/test_retry.py", "intent": "add regression test for ConnectionError"}
  ]
}
"""


def test_parse_valid_proposal() -> None:
    out = _parse_fix_proposal(_GOOD_FIX_JSON)
    assert out is not None
    assert out["files_touched"] == [
        "services/cart/redis_client.py",
        "services/cart/tests/test_retry.py",
    ]
    assert out["regression_test_added"] is True


def test_parse_strips_code_fence() -> None:
    fenced = f"Here's my proposal:\n```json\n{_GOOD_FIX_JSON}\n```\nReady to apply."
    out = _parse_fix_proposal(fenced)
    assert out is not None
    assert "reasoning" in out


def test_parse_missing_required_fields() -> None:
    assert _parse_fix_proposal('{"reasoning": "x"}') is None  # no files_touched
    assert _parse_fix_proposal('{"files_touched": []}') is None  # no reasoning


def test_parse_rejects_non_string_paths() -> None:
    payload = json.dumps({"reasoning": "x", "files_touched": [1, 2, 3]})
    assert _parse_fix_proposal(payload) is None


def test_parse_empty_or_garbage() -> None:
    assert _parse_fix_proposal("") is None
    assert _parse_fix_proposal("not json at all") is None
    assert _parse_fix_proposal("[unclosed") is None


def test_parse_array_form_not_accepted() -> None:
    """Fix proposal is a JSON OBJECT, not an array — different shape than hypotheses."""
    assert _parse_fix_proposal('[{"reasoning": "x"}]') is None


# ---------------------------------------------------------------------------- #
# User prompt builder                                                          #
# ---------------------------------------------------------------------------- #


def test_user_prompt_includes_diagnosis_and_action() -> None:
    diag = _diagnosis()
    prompt = _build_user_prompt(diag, FixAction.CODE_PATCH)
    assert "exp-aaaaaaaaaaaa" in prompt
    assert "code-patch" in prompt
    assert "missing-retry" in prompt
    # Output schema is described.
    assert "files_touched" in prompt
    assert "reasoning" in prompt


# ---------------------------------------------------------------------------- #
# Strategy guardrails                                                          #
# ---------------------------------------------------------------------------- #


def test_strategy_without_code_returns_explanatory_output() -> None:
    """Without a TargetCodeReader the strategy can't read the repo; surface that
    in the output instead of crashing."""
    strategy = ClaudeFixerStrategy()  # no code= passed
    out = asyncio.run(
        strategy.propose(diagnosis=_diagnosis(), intended_action=FixAction.CODE_PATCH)
    )
    assert isinstance(out, FixerOutput)
    assert out.files_touched == []
    assert out.regression_test_added is False
    assert "no TargetCodeReader" in out.reasoning


def test_strategy_defaults() -> None:
    s = ClaudeFixerStrategy()
    assert s.model == "claude-opus-4-7"
    assert s.max_turns > 0
    assert s.max_budget_usd > 0


def test_strategy_custom_config() -> None:
    s = ClaudeFixerStrategy(model="claude-haiku-4-5-20251001", max_turns=10, max_budget_usd=0.5)
    assert s.model == "claude-haiku-4-5-20251001"
    assert s.max_turns == 10
    assert s.max_budget_usd == 0.5


# ---------------------------------------------------------------------------- #
# Artifact persistence                                                         #
# ---------------------------------------------------------------------------- #


def test_artifact_written_to_configured_root(tmp_path: Path) -> None:
    """The strategy's _write_artifact() writes the parsed proposal to disk."""
    s = ClaudeFixerStrategy(artifact_root=tmp_path)
    parsed = json.loads(_GOOD_FIX_JSON)
    path = s._write_artifact("exp-aaaaaaaaaaaa", parsed)
    assert path is not None
    expected = tmp_path / "exp-aaaaaaaaaaaa" / "proposed" / "edits.json"
    assert path == expected
    assert expected.exists()
    written = json.loads(expected.read_text(encoding="utf-8"))
    assert written["reasoning"] == parsed["reasoning"]
    assert written["files_touched"] == parsed["files_touched"]


def test_artifact_directory_is_created(tmp_path: Path) -> None:
    s = ClaudeFixerStrategy(artifact_root=tmp_path / "new" / "deep" / "dir")
    parsed = json.loads(_GOOD_FIX_JSON)
    path = s._write_artifact("exp-aaaaaaaaaaaa", parsed)
    assert path is not None and path.exists()


def test_artifact_handles_unwritable_root_gracefully(tmp_path: Path) -> None:
    """If the artifact can't be written (e.g., readonly fs), return None — don't crash."""
    # Create a file where we'd want a directory; mkdir will fail and the write
    # path swallows OSError.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    s = ClaudeFixerStrategy(artifact_root=blocker)
    parsed = json.loads(_GOOD_FIX_JSON)
    path = s._write_artifact("exp-aaaaaaaaaaaa", parsed)
    assert path is None
