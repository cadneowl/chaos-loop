"""Tests for the meta harness: agent wrapping + invocation recording."""

from __future__ import annotations

import asyncio

import pytest

from agents._harness import Harness

# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


class _FakeAgent:
    """A minimal agent-like object with an async method and a sync property."""

    def __init__(self) -> None:
        self.model = "claude-fake-1"
        self.call_count = 0

    async def do(self, request: object) -> object:
        self.call_count += 1
        return _FakeReport(experiment_id=getattr(request, "experiment_id", "?"))

    async def explode(self, _request: object) -> object:
        raise RuntimeError("boom")

    def sync_method(self) -> int:
        return 42


class _FakeRequest:
    def __init__(self, experiment_id: str, kind: str = "baseline") -> None:
        self.experiment_id = experiment_id
        self.kind = kind


class _FakeReport:
    def __init__(self, experiment_id: str) -> None:
        self.experiment_id = experiment_id
        self.steady_state = True
        self.findings: list = []


# ---------------------------------------------------------------------------- #
# Wrap behavior                                                                #
# ---------------------------------------------------------------------------- #


def test_wrapped_agent_passes_through_async_call() -> None:
    inner = _FakeAgent()
    harness = Harness()
    wrapped = harness.instrument("fake", inner)

    result = asyncio.run(wrapped.do(_FakeRequest("exp-aaaaaaaaaaaa")))
    assert result.experiment_id == "exp-aaaaaaaaaaaa"
    assert inner.call_count == 1  # actually invoked the inner method


def test_invocation_recorded_with_summary() -> None:
    harness = Harness()
    wrapped = harness.instrument("fake", _FakeAgent())
    asyncio.run(wrapped.do(_FakeRequest("exp-aaaaaaaaaaaa")))

    assert len(harness.invocations) == 1
    inv = harness.invocations[0]
    assert inv.agent == "fake"
    assert inv.method == "do"
    assert inv.ok is True
    assert inv.duration_ms is not None and inv.duration_ms >= 0
    assert "experiment_id=exp-aaaaaaaaaaaa" in inv.input_summary
    assert inv.error is None
    # Output summary should include 'steady_state' since the fake report has it.
    assert "steady_state=True" in inv.output_summary
    assert "findings_count=0" in inv.output_summary


def test_error_recorded_but_propagated() -> None:
    harness = Harness()
    wrapped = harness.instrument("fake", _FakeAgent())

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(wrapped.explode(_FakeRequest("exp-aaaaaaaaaaaa")))

    assert len(harness.invocations) == 1
    inv = harness.invocations[0]
    assert inv.ok is False
    assert "boom" in (inv.error or "")
    # Duration is still captured even on failure.
    assert inv.duration_ms is not None


def test_sync_methods_not_wrapped() -> None:
    """Non-async attributes pass through unchanged; no invocation is recorded."""
    harness = Harness()
    wrapped = harness.instrument("fake", _FakeAgent())

    # Sync method call is direct.
    assert wrapped.sync_method() == 42
    # Property access is direct.
    assert wrapped.model == "claude-fake-1"
    # Neither produced an invocation log.
    assert harness.invocations == []


def test_invocations_ordered_by_call_sequence() -> None:
    harness = Harness()
    wrapped = harness.instrument("fake", _FakeAgent())

    asyncio.run(wrapped.do(_FakeRequest("exp-111111111111")))
    asyncio.run(wrapped.do(_FakeRequest("exp-222222222222")))
    asyncio.run(wrapped.do(_FakeRequest("exp-333333333333")))

    eids = [inv.input_summary for inv in harness.invocations]
    assert "exp-111111111111" in eids[0]
    assert "exp-222222222222" in eids[1]
    assert "exp-333333333333" in eids[2]


def test_wrapped_agent_is_read_only_proxy() -> None:
    """Callers can't accidentally re-bind attributes on the proxy."""
    wrapped = Harness().instrument("fake", _FakeAgent())
    with pytest.raises(AttributeError):
        wrapped.model = "tampered"  # type: ignore[misc]


def test_multiple_agents_share_one_harness() -> None:
    harness = Harness()
    a = harness.instrument("alpha", _FakeAgent())
    b = harness.instrument("beta", _FakeAgent())

    asyncio.run(a.do(_FakeRequest("exp-aaaaaaaaaaaa")))
    asyncio.run(b.do(_FakeRequest("exp-bbbbbbbbbbbb")))

    agents = [inv.agent for inv in harness.invocations]
    assert agents == ["alpha", "beta"]


# ---------------------------------------------------------------------------- #
# Integration with the orchestrator                                            #
# ---------------------------------------------------------------------------- #


def test_invocations_attached_to_experiment_record(tmp_path) -> None:
    """End-to-end: wrap mock agents, run the orchestrator, assert invocations
    landed on the persisted ExperimentRecord."""
    import yaml

    from agents._mocks import build_mock_agents
    from orchestrator.loop import Agents, ExperimentRunner
    from orchestrator.store import ExperimentStore
    from shared.contracts import ExperimentPlan

    harness = Harness()
    mocks = build_mock_agents()
    wrapped = {name: harness.instrument(name, inst) for name, inst in mocks.items()}
    agents = Agents(**wrapped)  # type: ignore[arg-type]

    plan = ExperimentPlan.model_validate(
        yaml.safe_load(
            (
                tmp_path.parent
                / "test_invocations_attached_to_experiment_recor0"
            ).parts  # this branch never executes; we use the example file below
            if False
            else (
                __import__("pathlib")
                .Path(__file__)
                .resolve()
                .parents[1]
                / "experiments/examples/01-redis-network-loss.yaml"
            ).read_text()
        )
    )

    store = ExperimentStore(tmp_path / "store.sqlite")
    runner = ExperimentRunner(agents=agents, store=store, harness=harness)
    record = asyncio.run(runner.run(plan))

    # The mock loop runs: tester.baseline, security.baseline, chaos.execute,
    # tester.verify, security.verify, diagnostician.diagnose, fixer.propose_fix.
    invoked = [(inv.agent, inv.method) for inv in record.agent_invocations]
    assert ("tester", "baseline") in invoked
    assert ("security", "baseline") in invoked
    assert ("chaos", "execute") in invoked
    assert ("tester", "verify") in invoked
    assert ("security", "verify") in invoked
    assert ("diagnostician", "diagnose") in invoked
    assert ("fixer", "propose_fix") in invoked

    # Each invocation has captured duration + ok flag.
    for inv in record.agent_invocations:
        assert inv.duration_ms is not None
        assert inv.ok is True
