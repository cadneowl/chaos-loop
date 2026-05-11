"""Tests for the agent factory wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents._factory import AgentConfig, build_real_agents
from agents.chaos.agent import ClaudeChaosAgent
from agents.chaos.cluster import FakeClusterIO
from agents.diagnostician.agent import ClaudeDiagnosticianAgent
from agents.diagnostician.diagnoser import FixtureDiagnoser
from agents.fixer.agent import ClaudeFixerAgent
from agents.fixer.strategy import FixerOutput, FixtureFixerStrategy
from agents.security.agent import ClaudeSecurityAgent
from agents.security.runner import FixtureRunner
from agents.tester.agent import ClaudeTesterAgent
from agents.tester.tools.prometheus import FixturePromBackend


def test_build_real_agents_produces_real_classes() -> None:
    """All five agents are concrete ClaudeXAgent instances, not mocks."""
    agents = build_real_agents(AgentConfig())
    assert isinstance(agents.tester, ClaudeTesterAgent)
    assert isinstance(agents.chaos, ClaudeChaosAgent)
    assert isinstance(agents.security, ClaudeSecurityAgent)
    assert isinstance(agents.diagnostician, ClaudeDiagnosticianAgent)
    assert isinstance(agents.fixer, ClaudeFixerAgent)


def test_overrides_take_precedence_over_env() -> None:
    """Test override path: pass fixture backends directly."""
    prom = FixturePromBackend()
    cluster = FakeClusterIO()
    runner = FixtureRunner()
    diagnoser = FixtureDiagnoser([])
    strategy = FixtureFixerStrategy(FixerOutput(reasoning="t", files_touched=["src/x.py"]))

    cfg = AgentConfig(prom_url="http://does-not-matter")
    agents = build_real_agents(
        cfg,
        prom_backend=prom,
        cluster=cluster,
        scanner_runner=runner,
        diagnoser=diagnoser,
        fixer_strategy=strategy,
    )

    # Tester's prom backend should be our fixture, not an HttpxPromBackend
    # built from the config URL.
    assert agents.tester._prom is prom  # type: ignore[attr-defined]
    assert agents.chaos._cluster is cluster  # type: ignore[attr-defined]


def test_factory_used_via_orchestrator_loop(tmp_path: Path) -> None:
    """End-to-end: build all agents with fixture backends and drive the orchestrator."""
    import yaml

    from agents.diagnostician.tools.code_reader import TargetCodeReader
    from orchestrator.loop import ExperimentRunner
    from orchestrator.store import ExperimentStore
    from shared.contracts import ExperimentPlan, RootCauseHypothesis

    # Build agents with fixtures pre-loaded to simulate a healthy-then-regressed flow.
    healthy = FixturePromBackend({
        ('up{job="prometheus"}', "instant"): [{"value": [0, "1"], "labels": {}}],
        ("quantile(0.95, scrape_duration_seconds) * 1000", "instant"): [
            {"value": [0, "45"], "labels": {}}
        ],
    })

    diagnoser = FixtureDiagnoser([
        RootCauseHypothesis(
            summary="test diagnosis",
            confidence=0.8,
            evidence=["fixture evidence"],
            suggested_fix_class="working-as-intended",
            affected_paths=[],
        )
    ])

    code_root = tmp_path / "repo"
    code_root.mkdir()

    agents = build_real_agents(
        AgentConfig(),
        prom_backend=healthy,
        cluster=FakeClusterIO(),
        scanner_runner=FixtureRunner(),
        diagnoser=diagnoser,
        code_reader=TargetCodeReader(code_root),
        # fixer_strategy not needed when diagnosis routes to working-as-intended
    )

    # Build a synthetic-target plan that uses zero quiet windows and tiny duration
    # so the run finishes instantly with the no-sleep we'll inject.
    plan_dict = yaml.safe_load((Path(__file__).resolve().parents[1] /
                                "experiments/examples/01-redis-network-loss.yaml").read_text())
    # Override target_app to "synthetic" so the tester loads the synthetic probe set
    # for which we have fixtures above.
    plan_dict["target_app"] = "synthetic"
    plan_dict["quiet_window_pre_seconds"] = 0
    plan_dict["quiet_window_post_seconds"] = 0
    plan_dict["faults"][0]["duration_seconds"] = 1
    plan = ExperimentPlan.model_validate(plan_dict)

    # Inject no-sleep into the chaos agent so duration=1s doesn't actually wait.
    async def no_sleep(_: float) -> None:
        return None

    agents.chaos._sleep = no_sleep  # type: ignore[attr-defined]

    # Configure fixer runs_dir so it writes the working-as-intended doc to tmp.
    agents.fixer._runs_dir = tmp_path / "runs"  # type: ignore[attr-defined]

    store = ExperimentStore(tmp_path / "store.sqlite")
    runner = ExperimentRunner(agents=agents, store=store)

    record = asyncio.run(runner.run(plan))

    # Steady security baseline + healthy tester -> chaos -> verify (still healthy
    # because we registered the same fixtures) -> STEADY. So no regression, no
    # diagnose. Verify the run completed and persisted.
    assert record.fix_proposal is None  # no regression -> no fixer invocation
    assert record.tester_baseline is not None
    assert record.tester_baseline.steady_state is True
    assert record.chaos_timeline is not None
    assert record.chaos_timeline.success is True
