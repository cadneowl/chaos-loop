"""Drives the full orchestrator loop end-to-end with mock agents."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from agents._mocks import build_mock_agents
from orchestrator.loop import Agents, ExperimentRunner
from orchestrator.store import ExperimentStore
from shared.contracts import ExperimentPlan, ExperimentState


@pytest.fixture
def store(tmp_path: Path) -> ExperimentStore:
    return ExperimentStore(tmp_path / "experiments.sqlite")


def _plan() -> ExperimentPlan:
    examples = Path(__file__).resolve().parents[1] / "experiments" / "examples"
    raw = yaml.safe_load((examples / "01-redis-network-loss.yaml").read_text())
    return ExperimentPlan.model_validate(raw)


def test_dry_run_full_loop(store: ExperimentStore) -> None:
    runner = ExperimentRunner(agents=Agents(**build_mock_agents()), store=store)
    record = asyncio.run(runner.run(_plan()))

    # Mock tester returns steady_state=False on verify -> regression path
    assert record.tester_baseline is not None
    assert record.tester_baseline.steady_state is True
    assert record.chaos_timeline is not None and record.chaos_timeline.success
    assert record.tester_verify is not None
    assert record.tester_verify.steady_state is False
    assert record.diagnosis is not None
    assert record.fix_proposal is not None
    assert record.fix_proposal.is_draft is True
    assert record.state == ExperimentState.RECORDED


def test_dry_run_persists(store: ExperimentStore) -> None:
    runner = ExperimentRunner(agents=Agents(**build_mock_agents()), store=store)
    plan = _plan()
    asyncio.run(runner.run(plan))

    loaded = store.load(plan.experiment_id)
    assert loaded is not None
    assert loaded.experiment_id == plan.experiment_id
    assert loaded.fix_proposal is not None
