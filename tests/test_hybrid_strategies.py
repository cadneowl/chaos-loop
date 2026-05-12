"""Tests for the three Hybrid wrappers (Static + optional LLM, with fallback)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agents.diagnostician.diagnoser import (
    FixtureDiagnoser,
    HybridDiagnoser,
    StaticDiagnoser,
    _are_root_cause_duplicates,
    _merge_root_cause_hypotheses,
)
from agents.fixer.strategy import (
    FixerOutput,
    FixtureFixerStrategy,
    HybridFixerStrategy,
    StaticFixerStrategy,
)
from agents.tester.hypothesizer import (
    FixtureHypothesizer,
    HybridHypothesizer,
    _are_duplicates,
    _merge_hypotheses,
    _normalize_ref,
)
from shared.contracts import (
    ChaosTimeline,
    DiagnosisReport,
    DiagnosisRequest,
    FixAction,
    Hypothesis,
    RootCauseHypothesis,
    TesterReport,
    TimelineEvent,
)

# =========================================================================== #
# Hypothesis merge primitives                                                 #
# =========================================================================== #


def _h(
    *,
    hid: str = "h-x-001",
    fault: str = "network.loss",
    refs: list[str] | None = None,
    confidence: float = 0.5,
) -> Hypothesis:
    return Hypothesis(
        id=hid,
        statement="x must y",
        rationale="r",
        proposed_fault=fault,
        success_criteria=["c"],
        confidence=confidence,
        code_references=refs if refs is not None else ["src/a.py:42"],
    )


def test_normalize_ref_strips_line_numbers() -> None:
    assert _normalize_ref("src/a.py:42") == "src/a.py"
    assert _normalize_ref("src/a.py:42-55") == "src/a.py"
    assert _normalize_ref("src\\a.py:1") == "src/a.py"


def test_are_duplicates_same_fault_same_file() -> None:
    a = _h(refs=["src/a.py:10"])
    b = _h(refs=["src/a.py:50"])
    assert _are_duplicates(a, b)


def test_are_not_duplicates_different_fault() -> None:
    a = _h(fault="network.loss")
    b = _h(fault="pod.kill")
    assert not _are_duplicates(a, b)


def test_are_not_duplicates_different_files() -> None:
    a = _h(refs=["src/a.py:1"])
    b = _h(refs=["src/b.py:1"])
    assert not _are_duplicates(a, b)


def test_merge_keeps_higher_confidence_for_duplicate() -> None:
    a = _h(hid="h-static-1", confidence=0.5)
    b = _h(hid="h-llm-1", confidence=0.9)
    merged = _merge_hypotheses([a], [b])
    assert len(merged) == 1
    assert merged[0].id == "h-llm-1"


def test_merge_keeps_static_when_higher_confidence() -> None:
    a = _h(hid="h-static-1", confidence=0.9)
    b = _h(hid="h-llm-1", confidence=0.5)
    merged = _merge_hypotheses([a], [b])
    assert merged[0].id == "h-static-1"


def test_merge_keeps_distinct_hypotheses() -> None:
    a = _h(hid="h-1", fault="network.loss", refs=["src/a.py:1"])
    b = _h(hid="h-2", fault="pod.kill", refs=["src/b.py:1"])
    merged = _merge_hypotheses([a], [b])
    assert {h.id for h in merged} == {"h-1", "h-2"}


def test_merge_results_ranked_by_confidence_desc() -> None:
    a = _h(hid="h-1", fault="network.loss", confidence=0.4)
    b = _h(hid="h-2", fault="pod.kill", confidence=0.9)
    c = _h(hid="h-3", fault="dns.error", confidence=0.6)
    merged = _merge_hypotheses([a, b], [c])
    assert [h.confidence for h in merged] == [0.9, 0.6, 0.4]


# =========================================================================== #
# HybridHypothesizer behavior                                                 #
# =========================================================================== #


def test_hybrid_hypothesizer_static_only_when_no_llm() -> None:
    static = FixtureHypothesizer([_h(hid="h-1")])
    hybrid = HybridHypothesizer(static=static, llm=None)
    out = asyncio.run(
        hybrid.generate(target_app="x", target_repo=None, code=None)
    )
    assert [h.id for h in out] == ["h-1"]


def test_hybrid_hypothesizer_merges_static_and_llm() -> None:
    static = FixtureHypothesizer([_h(hid="h-static", refs=["src/a.py:1"])])
    llm = FixtureHypothesizer([_h(hid="h-llm", fault="pod.kill", refs=["src/b.py:1"])])
    hybrid = HybridHypothesizer(static=static, llm=llm)
    out = asyncio.run(hybrid.generate(target_app="x", target_repo=None, code=None))
    assert {h.id for h in out} == {"h-static", "h-llm"}


def test_hybrid_hypothesizer_falls_back_when_llm_raises() -> None:
    static = FixtureHypothesizer([_h(hid="h-static")])

    async def boom(*_a, **_kw) -> list[Hypothesis]:
        raise RuntimeError("LLM exploded")

    llm = FixtureHypothesizer(boom)
    hybrid = HybridHypothesizer(static=static, llm=llm)
    out = asyncio.run(hybrid.generate(target_app="x", target_repo=None, code=None))
    assert [h.id for h in out] == ["h-static"]  # didn't crash; static kept


def test_hybrid_hypothesizer_dedupes_same_finding_higher_conf_wins() -> None:
    static_h = _h(hid="h-static", confidence=0.5)
    llm_h = _h(hid="h-llm", confidence=0.9)  # same fault, same file
    hybrid = HybridHypothesizer(
        static=FixtureHypothesizer([static_h]),
        llm=FixtureHypothesizer([llm_h]),
    )
    out = asyncio.run(hybrid.generate(target_app="x", target_repo=None, code=None))
    assert len(out) == 1
    assert out[0].id == "h-llm"


# =========================================================================== #
# Diagnoser merge primitives                                                  #
# =========================================================================== #


def _rch(
    *,
    summary: str = "x",
    confidence: float = 0.5,
    fix_class: str = "missing-retry",
    paths: list[str] | None = None,
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=confidence,
        evidence=["e"],
        suggested_fix_class=fix_class,  # type: ignore[arg-type]
        affected_paths=paths if paths is not None else [],
    )


def test_root_cause_duplicates_same_class_same_path() -> None:
    a = _rch(paths=["src/a.py"])
    b = _rch(paths=["src/a.py"])
    assert _are_root_cause_duplicates(a, b)


def test_root_cause_duplicates_same_class_no_paths_either_side() -> None:
    """Both lacking affected_paths -> treat as duplicate (same generic class)."""
    a = _rch()
    b = _rch()
    assert _are_root_cause_duplicates(a, b)


def test_root_cause_not_duplicates_different_class() -> None:
    a = _rch(fix_class="missing-retry")
    b = _rch(fix_class="missing-timeout")
    assert not _are_root_cause_duplicates(a, b)


def test_root_cause_not_duplicates_different_paths() -> None:
    a = _rch(fix_class="missing-retry", paths=["src/a.py"])
    b = _rch(fix_class="missing-retry", paths=["src/b.py"])
    assert not _are_root_cause_duplicates(a, b)


def test_merge_root_cause_higher_confidence_wins() -> None:
    a = _rch(summary="static-version", confidence=0.4, paths=["src/a.py"])
    b = _rch(summary="llm-version", confidence=0.85, paths=["src/a.py"])
    merged = _merge_root_cause_hypotheses([a], [b])
    assert len(merged) == 1
    assert merged[0].summary == "llm-version"


# =========================================================================== #
# HybridDiagnoser behavior                                                    #
# =========================================================================== #


def _diag_request() -> DiagnosisRequest:
    """Minimal request: needs a failed report + a chaos timeline."""
    ts = datetime.now(tz=UTC)
    return DiagnosisRequest(
        experiment_id="exp-aaaaaaaaaaaa",
        failed_tester_report=TesterReport(
            request_kind="verify",
            experiment_id="exp-aaaaaaaaaaaa",
            steady_state=False,
            failed_probes=["x"],
        ),
        chaos_timeline=ChaosTimeline(
            experiment_id="exp-aaaaaaaaaaaa",
            events=[
                TimelineEvent(timestamp=ts, fault_name="network.loss", event="started"),
            ],
            success=True,
        ),
    )


def test_hybrid_diagnoser_static_only_when_no_llm() -> None:
    static = FixtureDiagnoser([_rch(summary="from-static")])
    hybrid = HybridDiagnoser(static=static, llm=None)
    out = asyncio.run(hybrid.diagnose(request=_diag_request()))
    assert [r.summary for r in out] == ["from-static"]


def test_hybrid_diagnoser_merges_static_and_llm() -> None:
    static = FixtureDiagnoser([_rch(summary="static-claim", paths=["src/a.py"])])
    llm = FixtureDiagnoser([
        _rch(summary="llm-claim", fix_class="auth-control-gap", paths=["src/b.py"])
    ])
    hybrid = HybridDiagnoser(static=static, llm=llm)
    out = asyncio.run(hybrid.diagnose(request=_diag_request()))
    summaries = {r.summary for r in out}
    assert "static-claim" in summaries
    assert "llm-claim" in summaries


def test_hybrid_diagnoser_falls_back_when_llm_raises() -> None:
    static = FixtureDiagnoser([_rch(summary="static-only")])

    async def boom(_req: DiagnosisRequest) -> list[RootCauseHypothesis]:
        raise RuntimeError("LLM exploded")

    llm = FixtureDiagnoser(boom)
    hybrid = HybridDiagnoser(static=static, llm=llm)
    out = asyncio.run(hybrid.diagnose(request=_diag_request()))
    assert [r.summary for r in out] == ["static-only"]


def test_hybrid_diagnoser_caps_at_max_hypotheses() -> None:
    static = FixtureDiagnoser([
        _rch(summary=f"s-{i}", confidence=0.1 + i * 0.05, fix_class="missing-retry",
             paths=[f"src/{i}.py"])
        for i in range(10)
    ])
    hybrid = HybridDiagnoser(static=static, max_hypotheses=3)
    out = asyncio.run(hybrid.diagnose(request=_diag_request()))
    assert len(out) == 3


def test_hybrid_diagnoser_with_real_static_and_llm_augmenting() -> None:
    """Realistic flow: real StaticDiagnoser + a fixture 'LLM' adds a finding the rules can't see."""
    llm_extra = _rch(
        summary="LLM saw a code-patch case static doesn't model",
        confidence=0.75,
        fix_class="code-patch",
        paths=["services/cart/handler.py"],
    )
    hybrid = HybridDiagnoser(
        static=StaticDiagnoser(),
        llm=FixtureDiagnoser([llm_extra]),
    )
    out = asyncio.run(hybrid.diagnose(request=_diag_request()))
    fix_classes = {r.suggested_fix_class for r in out}
    assert "code-patch" in fix_classes  # from LLM
    assert "missing-retry" in fix_classes  # from Static (network.loss)


# =========================================================================== #
# HybridFixerStrategy behavior                                                #
# =========================================================================== #


def _diag_with_top_fix_class(fix_class: str) -> DiagnosisReport:
    return DiagnosisReport(
        experiment_id="exp-aaaaaaaaaaaa",
        hypotheses=[_rch(summary="top", fix_class=fix_class, paths=["src/a.py"])],
    )


def test_hybrid_fixer_static_only_when_no_llm() -> None:
    hybrid = HybridFixerStrategy(static=StaticFixerStrategy(), llm=None)
    out = asyncio.run(
        hybrid.propose(
            diagnosis=_diag_with_top_fix_class("missing-retry"),
            intended_action=FixAction.CODE_PATCH,
        )
    )
    # Static produced something based on the template.
    assert out.files_touched
    assert "tenacity" in out.reasoning.lower() or "retry" in out.reasoning.lower()


def test_hybrid_fixer_prefers_llm_when_it_returns_useful_output() -> None:
    """LLM gives a better proposal -> use it instead of the template."""
    llm_out = FixerOutput(
        reasoning="LLM hand-rolled a precise patch with the actual diff.",
        files_touched=["services/cart/redis_client.py"],
        regression_test_added=True,
    )
    hybrid = HybridFixerStrategy(
        static=StaticFixerStrategy(),
        llm=FixtureFixerStrategy(llm_out),
    )
    out = asyncio.run(
        hybrid.propose(
            diagnosis=_diag_with_top_fix_class("missing-retry"),
            intended_action=FixAction.CODE_PATCH,
        )
    )
    assert out.reasoning == llm_out.reasoning  # LLM output, not Static
    assert "tenacity" not in out.reasoning  # didn't fall through to template


def test_hybrid_fixer_falls_back_when_llm_raises() -> None:
    async def boom(_d: DiagnosisReport, _a: FixAction) -> FixerOutput:
        raise RuntimeError("LLM exploded")

    hybrid = HybridFixerStrategy(
        static=StaticFixerStrategy(),
        llm=FixtureFixerStrategy(boom),
    )
    out = asyncio.run(
        hybrid.propose(
            diagnosis=_diag_with_top_fix_class("missing-retry"),
            intended_action=FixAction.CODE_PATCH,
        )
    )
    # Static template fired.
    assert "tenacity" in out.reasoning.lower() or "retry" in out.reasoning.lower()


def test_hybrid_fixer_falls_back_when_llm_returns_empty() -> None:
    """If the LLM output is empty (no files, no reasoning), don't trust it."""
    empty = FixerOutput(reasoning="", files_touched=[], regression_test_added=False)
    hybrid = HybridFixerStrategy(
        static=StaticFixerStrategy(),
        llm=FixtureFixerStrategy(empty),
    )
    out = asyncio.run(
        hybrid.propose(
            diagnosis=_diag_with_top_fix_class("missing-retry"),
            intended_action=FixAction.CODE_PATCH,
        )
    )
    assert out.reasoning  # static populated it
    assert out.files_touched


@pytest.mark.parametrize("fix_class", ["missing-retry", "missing-timeout", "auth-control-gap"])
def test_hybrid_fixer_hands_through_intended_action(fix_class: str) -> None:
    captured: dict = {}

    async def capture(diagnosis: DiagnosisReport, action: FixAction) -> FixerOutput:
        captured["action"] = action
        return FixerOutput(reasoning=f"llm: {fix_class}", files_touched=["x.py"])

    hybrid = HybridFixerStrategy(
        static=StaticFixerStrategy(),
        llm=FixtureFixerStrategy(capture),
    )
    asyncio.run(
        hybrid.propose(
            diagnosis=_diag_with_top_fix_class(fix_class),
            intended_action=FixAction.CODE_PATCH,
        )
    )
    assert captured["action"] == FixAction.CODE_PATCH
