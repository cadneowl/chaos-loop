"""Orchestrator loop honors pause / resume / abort control signals.

Drives the loop with mock agents + a real SQLite store + a tiny pause-poll
interval so we don't actually sleep for seconds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from agents._mocks import build_mock_agents
from orchestrator.loop import Agents, ExperimentRunner
from orchestrator.store import ExperimentStore
from shared.contracts import (
    AbortReason,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentState,
)


def _load_plan(experiment_id: str = "exp-aaaaaaaaaaaa") -> ExperimentPlan:
    """Load the canonical example plan, normalize for tests, return."""
    plan_path = (
        Path(__file__).resolve().parents[1]
        / "experiments/examples/01-redis-network-loss.yaml"
    )
    raw = yaml.safe_load(plan_path.read_text())
    raw["experiment_id"] = experiment_id
    raw["safety"]["require_namespace_annotation"] = False
    return ExperimentPlan.model_validate(raw)


def _runner(tmp_path: Path) -> tuple[ExperimentRunner, ExperimentStore]:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    agents = Agents(**build_mock_agents())  # type: ignore[arg-type]
    runner = ExperimentRunner(agents=agents, store=store, pause_poll_interval_s=0.01)
    return runner, store


# --------------------------------------------------------------------------- #
# Abort                                                                       #
# --------------------------------------------------------------------------- #


def test_abort_request_stops_the_loop_at_next_boundary(tmp_path: Path) -> None:
    """Pre-set the abort flag before run() — the loop must abort at the first
    control-poll instead of completing."""
    runner, store = _runner(tmp_path)
    plan = _load_plan()

    async def go() -> ExperimentRecord:
        # Save a stub row so we can set the abort flag.
        store.save(ExperimentRecord(experiment_id=plan.experiment_id, plan=plan,
                                     state=ExperimentState.INITIALIZING))
        store.request_abort(plan.experiment_id, AbortReason.USER_KILL)
        return await runner.run(plan)

    record = asyncio.run(go())
    assert record.state == ExperimentState.ABORTED
    assert record.abort_reason == AbortReason.USER_KILL
    # The chaos.execute step must NOT have fired (abort hit first).
    chaos_calls = [
        ev for ev in (record.chaos_timeline.events if record.chaos_timeline else [])
    ]
    assert chaos_calls == []


def test_abort_with_explicit_reason_lands_on_record(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    plan = _load_plan(experiment_id="exp-bbbbbbbbbbbb")

    async def go() -> ExperimentRecord:
        store.save(ExperimentRecord(experiment_id=plan.experiment_id, plan=plan,
                                     state=ExperimentState.INITIALIZING))
        store.request_abort(plan.experiment_id, AbortReason.BUDGET_EXCEEDED)
        return await runner.run(plan)

    record = asyncio.run(go())
    assert record.abort_reason == AbortReason.BUDGET_EXCEEDED


# --------------------------------------------------------------------------- #
# Pause / resume                                                              #
# --------------------------------------------------------------------------- #


def test_pause_request_blocks_the_loop_until_cleared(tmp_path: Path) -> None:
    """Set pause before the loop runs; have a separate task observe the
    persisted PAUSED state, then clear the pause; assert the loop only
    completes after the pause was cleared."""
    runner, store = _runner(tmp_path)
    plan = _load_plan(experiment_id="exp-cccccccccccc")
    observed_paused = asyncio.Event()

    async def observe_then_clear() -> None:
        # Poll until we see PAUSED actually persisted to the store, then clear.
        # If we never see PAUSED, the test fails the explicit assertion below.
        for _ in range(200):  # 200 * 0.01s = 2s max wait
            await asyncio.sleep(0.01)
            loaded = store.load(plan.experiment_id)
            if loaded is not None and loaded.state == ExperimentState.PAUSED:
                observed_paused.set()
                break
        store.set_pause(plan.experiment_id, False)

    async def go() -> tuple[ExperimentRecord, ExperimentState]:
        store.save(ExperimentRecord(experiment_id=plan.experiment_id, plan=plan,
                                     state=ExperimentState.INITIALIZING))
        store.set_pause(plan.experiment_id, True)
        observer = asyncio.create_task(observe_then_clear())
        record = await runner.run(plan)
        await observer
        loaded = store.load(plan.experiment_id)
        assert loaded is not None
        return record, loaded.state

    record, final_state = asyncio.run(go())
    # The store passed through PAUSED before the loop completed.
    assert observed_paused.is_set(), "loop never persisted PAUSED state"
    # The mock loop completes successfully: regression detected, fix proposed.
    assert record.state == ExperimentState.RECORDED
    assert final_state == ExperimentState.RECORDED


def test_terminal_finish_clears_control_flags(tmp_path: Path) -> None:
    """A normal completion must leave no stale operator flags on the row."""
    runner, store = _runner(tmp_path)
    plan = _load_plan(experiment_id="exp-fffffffffff0")
    asyncio.run(runner.run(plan))
    ctrl = store.load_control(plan.experiment_id)
    assert ctrl.pause_requested is False
    assert ctrl.abort_requested is False
    assert ctrl.abort_reason is None


def test_terminal_abort_clears_control_flags(tmp_path: Path) -> None:
    """Same property after an abort path: no leftover flags."""
    runner, store = _runner(tmp_path)
    plan = _load_plan(experiment_id="exp-fffffffffff1")

    async def go() -> None:
        store.save(ExperimentRecord(experiment_id=plan.experiment_id, plan=plan,
                                     state=ExperimentState.INITIALIZING))
        store.request_abort(plan.experiment_id, AbortReason.USER_KILL)
        await runner.run(plan)

    asyncio.run(go())
    ctrl = store.load_control(plan.experiment_id)
    assert ctrl.pause_requested is False
    assert ctrl.abort_requested is False
    assert ctrl.abort_reason is None


def test_pause_then_abort_during_pause_aborts_cleanly(tmp_path: Path) -> None:
    """Pause is set, then operator decides to abort instead. The control
    loop sees the abort flag on the next poll and exits via the abort path."""
    runner, store = _runner(tmp_path)
    plan = _load_plan(experiment_id="exp-dddddddddddd")

    async def request_abort_after(delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        store.request_abort(plan.experiment_id, AbortReason.USER_KILL)

    async def go() -> ExperimentRecord:
        store.save(ExperimentRecord(experiment_id=plan.experiment_id, plan=plan,
                                     state=ExperimentState.INITIALIZING))
        store.set_pause(plan.experiment_id, True)
        aborter = asyncio.create_task(request_abort_after(0.15))
        record = await runner.run(plan)
        await aborter
        return record

    record = asyncio.run(go())
    assert record.state == ExperimentState.ABORTED
    assert record.abort_reason == AbortReason.USER_KILL


def test_no_pause_no_abort_runs_to_completion(tmp_path: Path) -> None:
    """Sanity check: the new control-poll doesn't break the happy path."""
    runner, _store = _runner(tmp_path)
    plan = _load_plan(experiment_id="exp-eeeeeeeeeeee")
    record = asyncio.run(runner.run(plan))
    assert record.state == ExperimentState.RECORDED
    assert record.abort_reason is None
