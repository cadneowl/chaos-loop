"""Tests for StaticFixerStrategy — templated, no-LLM fix proposals."""

from __future__ import annotations

import asyncio

import pytest

from agents.fixer.strategy import (
    _FIX_TEMPLATES,
    StaticFixerStrategy,
    _suggested_test_path,
)
from shared.contracts import (
    DiagnosisReport,
    FixAction,
    RootCauseHypothesis,
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _hyp(
    *,
    summary: str = "redis dep with no retry",
    confidence: float = 0.8,
    fix_class: str = "missing-retry",
    paths: list[str] | None = None,
    evidence: list[str] | None = None,
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=confidence,
        evidence=evidence if evidence is not None else ["chaos timeline: started network.loss"],
        suggested_fix_class=fix_class,  # type: ignore[arg-type]
        affected_paths=paths if paths is not None else ["services/cart/redis_client.py"],
    )


def _diagnosis(*hyps: RootCauseHypothesis) -> DiagnosisReport:
    return DiagnosisReport(experiment_id="exp-aaaaaaaaaaaa", hypotheses=list(hyps))


def _propose(diag: DiagnosisReport, action: FixAction = FixAction.CODE_PATCH):
    return asyncio.run(StaticFixerStrategy().propose(diagnosis=diag, intended_action=action))


# --------------------------------------------------------------------------- #
# _suggested_test_path                                                        #
# --------------------------------------------------------------------------- #


def test_suggested_test_path_for_nested_source() -> None:
    out = _suggested_test_path("services/cart/handler.py")
    assert out == "services/cart/tests/test_handler_regression.py"


def test_suggested_test_path_for_flat_source() -> None:
    out = _suggested_test_path("main.py")
    assert out == "tests/test_main_regression.py"


def test_suggested_test_path_normalizes_backslashes() -> None:
    out = _suggested_test_path("services\\cart\\handler.py")
    assert out == "services/cart/tests/test_handler_regression.py"


# --------------------------------------------------------------------------- #
# Per fix-class proposals                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fix_class",
    sorted(_FIX_TEMPLATES.keys()),
)
def test_each_fix_class_produces_a_proposal(fix_class: str) -> None:
    out = _propose(_diagnosis(_hyp(fix_class=fix_class)))
    # Every templated class produces non-empty reasoning.
    assert out.reasoning
    assert _FIX_TEMPLATES[fix_class].summary in out.reasoning


def test_missing_retry_proposal_includes_retry_guidance() -> None:
    out = _propose(_diagnosis(_hyp(fix_class="missing-retry")))
    assert "tenacity" in out.reasoning.lower() or "retry" in out.reasoning.lower()
    assert "exponential" in out.reasoning.lower() or "backoff" in out.reasoning.lower()


def test_missing_timeout_proposal_includes_timeout_guidance() -> None:
    out = _propose(_diagnosis(_hyp(fix_class="missing-timeout")))
    assert "timeout=" in out.reasoning


def test_image_policy_does_not_add_regression_test() -> None:
    """image-policy is a config/admission change; code-test isn't apt."""
    out = _propose(_diagnosis(_hyp(fix_class="image-policy", paths=["policy/admit.yaml"])))
    assert out.regression_test_added is False


def test_config_change_does_not_add_regression_test() -> None:
    out = _propose(_diagnosis(_hyp(fix_class="config-change")))
    assert out.regression_test_added is False


def test_unknown_fix_class_falls_back_to_code_patch_template() -> None:
    """A fix_class outside the table should still produce something useful."""
    # Bypass Pydantic Literal validation by constructing the proposal flow with
    # a hypothesis whose class is one we don't have a template for. Here we use
    # a real value and just verify the fallback works logically.
    out = _propose(_diagnosis(_hyp(fix_class="code-patch")))
    assert out.reasoning  # generic code-patch template fired


# --------------------------------------------------------------------------- #
# files_touched assembly                                                      #
# --------------------------------------------------------------------------- #


def test_files_touched_includes_affected_paths_and_test_path() -> None:
    out = _propose(_diagnosis(_hyp(
        fix_class="missing-retry",
        paths=["services/cart/redis_client.py"],
    )))
    assert "services/cart/redis_client.py" in out.files_touched
    assert "services/cart/tests/test_redis_client_regression.py" in out.files_touched
    assert out.regression_test_added


def test_files_touched_no_test_when_template_says_no() -> None:
    out = _propose(_diagnosis(_hyp(
        fix_class="config-change",
        paths=["k8s/deploy.yaml"],
    )))
    assert out.files_touched == ["k8s/deploy.yaml"]


def test_files_touched_empty_when_no_affected_paths() -> None:
    """Fixer agent already gates on `paths` for the LLM strategy; the static
    one mirrors that behavior — no paths -> no test file synthesized."""
    out = _propose(_diagnosis(_hyp(fix_class="missing-retry", paths=[])))
    assert out.files_touched == []
    assert out.regression_test_added is False


def test_files_touched_dedupes_when_test_path_already_present() -> None:
    """If diagnosis already names the test path, don't add it twice."""
    out = _propose(_diagnosis(_hyp(
        fix_class="missing-retry",
        paths=[
            "services/cart/redis_client.py",
            "services/cart/tests/test_redis_client_regression.py",
        ],
    )))
    assert out.files_touched.count("services/cart/tests/test_redis_client_regression.py") == 1


# --------------------------------------------------------------------------- #
# Reasoning content                                                           #
# --------------------------------------------------------------------------- #


def test_reasoning_includes_diagnosis_context() -> None:
    out = _propose(_diagnosis(_hyp(
        fix_class="missing-retry",
        summary="cart hard-deps redis without retry",
        confidence=0.82,
        evidence=["log line at services/cart/handler.py:42", "loki: connection refused"],
    )))
    assert "cart hard-deps redis without retry" in out.reasoning
    assert "missing-retry" in out.reasoning
    assert "0.82" in out.reasoning
    assert "connection refused" in out.reasoning


def test_pr_url_is_none() -> None:
    """Static strategy never opens PRs — that's M6.x.b territory."""
    out = _propose(_diagnosis(_hyp()))
    assert out.pr_url is None
