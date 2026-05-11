"""Tests for the fixer's PR body renderer."""

from __future__ import annotations

from agents.fixer.pr_template import confidence_bucket, render_pr_body
from shared.contracts import (
    DiagnosisReport,
    FixAction,
    FixProposal,
    RootCauseHypothesis,
)


def _diagnosis(*hyps: RootCauseHypothesis) -> DiagnosisReport:
    return DiagnosisReport(
        experiment_id="exp-aaaaaaaaaaaa",
        hypotheses=list(hyps),
        notes="test notes",
    )


def _hyp(
    summary: str = "cart hard-deps redis with no fallback",
    confidence: float = 0.8,
    fix_class: str = "missing-fallback",
    evidence: list[str] | None = None,
    paths: list[str] | None = None,
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=confidence,
        # Distinguish "not provided" (use default) from "explicitly empty" (use as-is).
        evidence=evidence if evidence is not None else ["log line at services/cart/handler.py:42"],
        suggested_fix_class=fix_class,  # type: ignore[arg-type]
        affected_paths=paths if paths is not None else ["services/cart/handler.py"],
    )


def _fix(
    action: FixAction = FixAction.CODE_PATCH,
    files: list[str] | None = None,
    pr_url: str | None = "https://example.invalid/pr/1",
    confidence: float = 0.8,
) -> FixProposal:
    return FixProposal(
        experiment_id="exp-aaaaaaaaaaaa",
        action=action,
        pr_url=pr_url,
        confidence=confidence,
        reasoning="Added an in-memory fallback when the cache call raises.",
        files_touched=(
            files
            if files is not None
            else ["services/cart/handler.py", "services/cart/tests/test_fallback.py"]
        ),
        regression_test_added=True,
    )


# ---------------------------------------------------------------------------- #
# confidence_bucket                                                            #
# ---------------------------------------------------------------------------- #


def test_confidence_buckets() -> None:
    assert confidence_bucket(0.0) == "low"
    assert confidence_bucket(0.49) == "low"
    assert confidence_bucket(0.5) == "med"
    assert confidence_bucket(0.79) == "med"
    assert confidence_bucket(0.8) == "high"
    assert confidence_bucket(1.0) == "high"


# ---------------------------------------------------------------------------- #
# render_pr_body                                                               #
# ---------------------------------------------------------------------------- #


def test_render_includes_core_metadata() -> None:
    body = render_pr_body(_diagnosis(_hyp()), _fix())
    assert "exp-aaaaaaaaaaaa" in body
    assert "missing-fallback" in body
    assert "code-patch" in body
    assert "0.80" in body
    assert "high" in body  # confidence bucket


def test_render_lists_files_touched() -> None:
    body = render_pr_body(_diagnosis(_hyp()), _fix())
    assert "`services/cart/handler.py`" in body
    assert "`services/cart/tests/test_fallback.py`" in body


def test_render_quotes_evidence() -> None:
    body = render_pr_body(_diagnosis(_hyp()), _fix())
    assert "services/cart/handler.py:42" in body


def test_render_uses_top_hypothesis() -> None:
    """If multiple hypotheses, the rendered body should describe the FIRST one."""
    top = _hyp(summary="TOP SUMMARY", confidence=0.9)
    other = _hyp(summary="other one", confidence=0.4, fix_class="missing-retry")
    body = render_pr_body(_diagnosis(top, other), _fix(confidence=0.9))
    assert "TOP SUMMARY" in body
    assert "other one" not in body


def test_render_handles_no_evidence() -> None:
    body = render_pr_body(_diagnosis(_hyp(evidence=[])), _fix())
    assert "no evidence cited" in body


def test_render_handles_no_files() -> None:
    body = render_pr_body(_diagnosis(_hyp()), _fix(files=[]))
    assert "no files touched" in body


def test_render_includes_draft_warning() -> None:
    """Reviewer must see that this is a draft and should not be merged blindly."""
    body = render_pr_body(_diagnosis(_hyp()), _fix())
    assert "Draft" in body
    assert "do not merge without review" in body.lower()


def test_render_includes_reviewer_checklist() -> None:
    body = render_pr_body(_diagnosis(_hyp()), _fix())
    assert "Reviewer checklist" in body
    # Standard items the human should answer.
    assert "Is the fix correct" in body
    assert "regression test" in body


def test_render_handles_missing_regression_test() -> None:
    body = render_pr_body(
        _diagnosis(_hyp()),
        _fix().model_copy(update={"regression_test_added": False}),
    )
    assert "No regression test added" in body
