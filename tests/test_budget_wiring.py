"""Budget tracking is a safety property — exhaustively tested here.

Covers the BudgetTracker primitive, the contextvar that attributes LLM cost
to the running agent invocation, and the loop's hard-cap abort path.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from agents._harness import (
    AgentInvocation,
    Harness,
    _current_invocation,
    record_llm_spend,
)
from orchestrator.budget import BudgetTracker
from shared.contracts import TokenBudget

# --------------------------------------------------------------------------- #
# BudgetTracker primitives                                                    #
# --------------------------------------------------------------------------- #


def _tracker(soft: float = 1.0, hard: float = 5.0, wall: int = 600) -> BudgetTracker:
    return BudgetTracker(TokenBudget(soft_cap_usd=soft, hard_cap_usd=hard, wall_clock_seconds=wall))


def test_record_spend_accumulates() -> None:
    t = _tracker()
    t.record_spend(0.5)
    t.record_spend(0.3)
    assert t.spent_usd == pytest.approx(0.8)


def test_soft_warn_due_fires_once_then_silent() -> None:
    t = _tracker(soft=1.0, hard=10.0)
    t.record_spend(0.5)
    assert t.soft_warn_due() is False
    t.record_spend(0.6)  # now 1.1, above soft
    assert t.soft_warn_due() is True
    # Second call returns False — we don't want a warning spam every step.
    assert t.soft_warn_due() is False


def test_hard_exceeded_on_dollars() -> None:
    t = _tracker(soft=1.0, hard=2.0)
    assert t.hard_exceeded() is False
    t.record_spend(2.5)
    assert t.hard_exceeded() is True


def test_hard_exceeded_on_wall_clock(monkeypatch) -> None:
    t = _tracker(wall=1)
    assert t.hard_exceeded() is False
    monkeypatch.setattr(time, "monotonic", lambda: t.started_at + 2)
    assert t.hard_exceeded() is True


# --------------------------------------------------------------------------- #
# Harness contextvar attribution                                              #
# --------------------------------------------------------------------------- #


def test_record_llm_spend_attributes_to_current_invocation() -> None:
    inv = AgentInvocation(agent="x", method="y", started_at_ms=0)
    token = _current_invocation.set(inv)
    try:
        record_llm_spend(0.5)
        record_llm_spend(0.25)
    finally:
        _current_invocation.reset(token)
    assert inv.spend_usd == pytest.approx(0.75)


def test_record_llm_spend_outside_invocation_is_noop() -> None:
    """Direct CLI use or tests have no current invocation — calls must not raise."""
    # Sanity: no current invocation set.
    assert _current_invocation.get() is None
    record_llm_spend(0.5)  # must not raise / blow up


def test_record_llm_spend_zero_is_ignored() -> None:
    """LiteLLM returns 0 for Ollama (unknown pricing). We don't dirty `spend_usd`
    with zero attributions; otherwise the field flips from None to 0.0 for
    profile=static runs that don't actually use the LLM."""
    inv = AgentInvocation(agent="x", method="y", started_at_ms=0)
    token = _current_invocation.set(inv)
    try:
        record_llm_spend(0.0)
    finally:
        _current_invocation.reset(token)
    assert inv.spend_usd is None


def test_harness_wrap_sets_current_invocation_during_call() -> None:
    """The wrapper must publish the AgentInvocation so record_llm_spend sees it."""
    harness = Harness()
    captured: list[float | None] = []

    class Agent:
        async def m(self) -> None:
            # Simulate a strategy calling complete_with_tools which calls record_llm_spend.
            record_llm_spend(1.25)
            inv = _current_invocation.get()
            captured.append(inv.spend_usd if inv else None)

    wrapped = harness.instrument("x", Agent())
    asyncio.run(wrapped.m())
    assert harness.invocations[0].spend_usd == pytest.approx(1.25)
    assert captured == [pytest.approx(1.25)]


# --------------------------------------------------------------------------- #
# Loop budget abort path                                                      #
# --------------------------------------------------------------------------- #


def test_loop_aborts_when_hard_cap_exceeded(tmp_path) -> None:
    """Inject a high spend during baseline; the loop must abort with
    BUDGET_EXCEEDED before chaos injection."""
    import yaml

    from agents._mocks import build_mock_agents
    from orchestrator.loop import Agents, ExperimentRunner
    from orchestrator.store import ExperimentStore
    from shared.contracts import AbortReason, ExperimentPlan, ExperimentState

    harness = Harness()
    mocks = build_mock_agents()

    # Replace the mock tester so its baseline attribution spend > hard cap.
    inner_tester = mocks["tester"]
    original_baseline = inner_tester.baseline

    async def baseline_with_spend(req):  # type: ignore[no-untyped-def]
        # Within the harness wrapper there's a current invocation; spend lands on it.
        record_llm_spend(99.0)
        return await original_baseline(req)

    inner_tester.baseline = baseline_with_spend  # type: ignore[method-assign]

    wrapped = {name: harness.instrument(name, inst) for name, inst in mocks.items()}
    agents = Agents(**wrapped)  # type: ignore[arg-type]

    plan_dict = yaml.safe_load(
        (
            __import__("pathlib")
            .Path(__file__)
            .resolve()
            .parents[1]
            / "experiments/examples/01-redis-network-loss.yaml"
        ).read_text()
    )
    # Set a tiny hard cap so 99 USD trips it.
    plan_dict["budget"]["soft_cap_usd"] = 0.5
    plan_dict["budget"]["hard_cap_usd"] = 1.0
    plan = ExperimentPlan.model_validate(plan_dict)

    store = ExperimentStore(tmp_path / "store.sqlite")
    runner = ExperimentRunner(agents=agents, store=store, harness=harness)
    record = asyncio.run(runner.run(plan))

    assert record.state == ExperimentState.ABORTED
    assert record.abort_reason == AbortReason.BUDGET_EXCEEDED
    assert record.spend_usd >= 99.0
    # finished_at must be set (M4 fix)
    assert record.finished_at is not None
    assert isinstance(record.finished_at, datetime)
    assert record.finished_at.tzinfo == UTC
    # The chaos.execute step must NOT have fired (annotation lookup is fine —
    # that's the namespace gate, which runs before baseline).
    assert not any(
        i.agent == "chaos" and i.method == "execute" for i in record.agent_invocations
    )
