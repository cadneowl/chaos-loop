"""Tests for hypothesis suppression."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator.suppression import (
    SuppressList,
    active_hypotheses,
    apply_to_diagnosis,
    build_active_list,
    hypothesis_fingerprint,
    load_repo_suppress_list,
    rule_matches,
)
from shared.contracts import (
    DiagnosisReport,
    ExperimentPlan,
    RootCauseHypothesis,
    SuppressionRule,
)


def hyp(
    *,
    summary: str = "cart service has hard dep on Redis with no retry",
    fix_class: str = "missing-retry",
    paths: list[str] | None = None,
    confidence: float = 0.8,
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=confidence,
        evidence=[],
        suggested_fix_class=fix_class,  # type: ignore[arg-type]
        affected_paths=paths or ["services/cart/redis_client.py"],
    )


def diagnosis(hypotheses: list[RootCauseHypothesis]) -> DiagnosisReport:
    return DiagnosisReport(
        experiment_id="exp-aaaaaaaaaaaa",
        hypotheses=hypotheses,
    )


# ---------------------------------------------------------------------- rules

def test_rule_requires_at_least_one_match_field() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SuppressionRule(reason="bare reason isn't enough")


def test_rule_matches_by_fix_class() -> None:
    rule = SuppressionRule(fix_class="missing-retry", reason="tracked in JIRA-1234")
    assert rule_matches(rule, hyp(), now=datetime.now(tz=UTC)) is True
    assert (
        rule_matches(rule, hyp(fix_class="missing-timeout"), now=datetime.now(tz=UTC)) is False
    )


def test_rule_matches_by_path_glob() -> None:
    rule = SuppressionRule(path_glob="services/legacy/*")
    legacy = hyp(paths=["services/legacy/cart.py", "services/cart/redis_client.py"])
    assert rule_matches(rule, legacy, now=datetime.now(tz=UTC)) is True
    fresh = hyp(paths=["services/cart/redis_client.py"])
    assert rule_matches(rule, fresh, now=datetime.now(tz=UTC)) is False


def test_rule_matches_by_summary_contains_case_insensitive() -> None:
    rule = SuppressionRule(summary_contains="REDIS")
    assert rule_matches(rule, hyp(), now=datetime.now(tz=UTC)) is True
    other = hyp(summary="cart has missing timeout on database call")
    assert rule_matches(rule, other, now=datetime.now(tz=UTC)) is False


def test_rule_matches_by_hypothesis_id() -> None:
    h = hyp()
    rule = SuppressionRule(hypothesis_id=hypothesis_fingerprint(h))
    assert rule_matches(rule, h, now=datetime.now(tz=UTC)) is True
    assert rule_matches(rule, hyp(fix_class="missing-timeout"), now=datetime.now(tz=UTC)) is False


def test_rule_expires_at_disables_match_after_deadline() -> None:
    past = datetime.now(tz=UTC) - timedelta(days=1)
    rule = SuppressionRule(fix_class="missing-retry", expires_at=past)
    assert rule_matches(rule, hyp(), now=datetime.now(tz=UTC)) is False


def test_rule_with_multiple_match_fields_requires_all_to_match() -> None:
    # AND across fields: hypothesis must satisfy every set match field.
    rule = SuppressionRule(fix_class="missing-retry", path_glob="services/legacy/*")
    assert rule_matches(rule, hyp(), now=datetime.now(tz=UTC)) is False  # path doesn't match
    legacy = hyp(paths=["services/legacy/cart.py"])
    assert rule_matches(rule, legacy, now=datetime.now(tz=UTC)) is True


# ----------------------------------------------------------------- fingerprint


def test_fingerprint_is_stable_across_calls() -> None:
    h = hyp()
    assert hypothesis_fingerprint(h) == hypothesis_fingerprint(h)


def test_fingerprint_differs_when_summary_changes() -> None:
    assert hypothesis_fingerprint(hyp()) != hypothesis_fingerprint(
        hyp(summary="cart service has hard dep on Postgres with no retry")
    )


def test_fingerprint_independent_of_path_order() -> None:
    a = hyp(paths=["a.py", "b.py", "c.py"])
    b = hyp(paths=["c.py", "a.py", "b.py"])
    assert hypothesis_fingerprint(a) == hypothesis_fingerprint(b)


# ----------------------------------------------------------------- apply_to


def test_apply_tags_suppressed_hypothesis() -> None:
    h = hyp()
    d = diagnosis([h])
    rules = SuppressList(rules=[SuppressionRule(fix_class="missing-retry", reason="known")])
    apply_to_diagnosis(d, rules)
    fp = hypothesis_fingerprint(h)
    assert d.suppressed_fingerprints == [fp]
    assert d.suppression_notes[fp] == "known"
    assert active_hypotheses(d) == []  # nothing left for the fixer


def test_apply_keeps_active_and_suppressed_in_parallel() -> None:
    h1 = hyp(summary="cart Redis dep", fix_class="missing-retry")
    h2 = hyp(summary="frontend timeout", fix_class="missing-timeout")
    d = diagnosis([h1, h2])
    rules = SuppressList(rules=[SuppressionRule(fix_class="missing-retry")])
    apply_to_diagnosis(d, rules)
    fp1 = hypothesis_fingerprint(h1)
    fp2 = hypothesis_fingerprint(h2)
    assert d.suppressed_fingerprints == [fp1]
    assert fp2 not in d.suppressed_fingerprints
    assert active_hypotheses(d) == [h2]


def test_apply_first_matching_rule_wins() -> None:
    # Order matters: the first rule that matches is the one recorded as the
    # reason, so audit trails point at the most specific rule the operator
    # has put first in the file.
    h = hyp()
    d = diagnosis([h])
    rules = SuppressList(
        rules=[
            SuppressionRule(fix_class="missing-retry", reason="known JIRA-1"),
            SuppressionRule(path_glob="services/*", reason="catch-all"),
        ]
    )
    apply_to_diagnosis(d, rules)
    fp = hypothesis_fingerprint(h)
    assert d.suppression_notes[fp] == "known JIRA-1"


def test_apply_describes_matcher_when_no_reason_given() -> None:
    h = hyp()
    d = diagnosis([h])
    rules = SuppressList(rules=[SuppressionRule(fix_class="missing-retry")])
    apply_to_diagnosis(d, rules)
    fp = hypothesis_fingerprint(h)
    assert "fix_class" in d.suppression_notes[fp]


# ------------------------------------------------------------- file + merge


def test_load_repo_returns_empty_when_file_missing(tmp_path: Path) -> None:
    out = load_repo_suppress_list(tmp_path)
    assert out.rules == []


def test_load_repo_parses_valid_yaml(tmp_path: Path) -> None:
    (tmp_path / ".chaos").mkdir()
    (tmp_path / ".chaos" / "suppress.yaml").write_text(
        """
        rules:
          - fix_class: missing-retry
            reason: tracked in JIRA-1234
          - path_glob: services/legacy/*
            reason: legacy code, do not touch
        """,
        encoding="utf-8",
    )
    out = load_repo_suppress_list(tmp_path)
    assert len(out.rules) == 2
    assert out.rules[0].fix_class == "missing-retry"
    assert out.rules[1].path_glob == "services/legacy/*"


def test_load_repo_rejects_invalid_schema(tmp_path: Path) -> None:
    (tmp_path / ".chaos").mkdir()
    # Rule with no match field — should raise loudly, not silently parse.
    (tmp_path / ".chaos" / "suppress.yaml").write_text(
        """
        rules:
          - reason: this has no matcher
        """,
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="at least one"):
        load_repo_suppress_list(tmp_path)


def test_build_active_merges_repo_and_plan(tmp_path: Path) -> None:
    (tmp_path / ".chaos").mkdir()
    (tmp_path / ".chaos" / "suppress.yaml").write_text(
        "rules:\n  - fix_class: missing-retry\n    reason: repo\n",
        encoding="utf-8",
    )
    plan = _minimal_plan(
        suppress=[SuppressionRule(path_glob="services/legacy/*", reason="plan")]
    )
    out = build_active_list(plan, repo_root=tmp_path)
    assert len(out.rules) == 2
    # Repo first, then plan — preserves explicit ordering for audit.
    assert out.rules[0].reason == "repo"
    assert out.rules[1].reason == "plan"


# ------------------------------------------------------------ plan helpers


def _minimal_plan(suppress: list[SuppressionRule] | None = None) -> ExperimentPlan:
    """A bare-minimum plan for tests that need an `ExperimentPlan` instance."""
    from shared.contracts import FaultSpec, SafetyConstraints

    return ExperimentPlan(
        title="t",
        target_app="otel-demo",
        faults=[
            FaultSpec(
                category="network",  # type: ignore[arg-type]
                name="network.loss",
                target_selector={"app": "x"},
                parameters={},
                duration_seconds=10,
                requires_approval=False,
                rationale="r",
            )
        ],
        safety=SafetyConstraints(
            cluster_context="kind-chaos-dev",
            namespace="otel-demo",
            require_namespace_annotation=False,
        ),
        suppress=list(suppress or []),
    )
