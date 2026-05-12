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


# --------------------------------------------------------------------------- #
# Profile selection                                                           #
# --------------------------------------------------------------------------- #


def test_profile_static_wires_static_strategies_everywhere() -> None:
    """profile=static -> StaticHypothesizer / StaticDiagnoser / StaticFixerStrategy."""
    from agents.diagnostician.diagnoser import StaticDiagnoser
    from agents.fixer.strategy import StaticFixerStrategy
    from agents.tester.hypothesizer import StaticHypothesizer

    agents = build_real_agents(AgentConfig(), profile="static")
    assert isinstance(agents.tester._hypothesizer, StaticHypothesizer)  # type: ignore[attr-defined]
    assert isinstance(agents.diagnostician._diagnoser, StaticDiagnoser)  # type: ignore[attr-defined]
    assert isinstance(agents.fixer._strategy, StaticFixerStrategy)  # type: ignore[attr-defined]


def test_profile_hybrid_wires_hybrid_wrappers_everywhere() -> None:
    """profile=hybrid -> Hybrid* wrappers that wrap Static + LLM."""
    from agents.diagnostician.diagnoser import HybridDiagnoser
    from agents.fixer.strategy import HybridFixerStrategy
    from agents.tester.hypothesizer import HybridHypothesizer

    agents = build_real_agents(AgentConfig(), profile="hybrid")
    assert isinstance(agents.tester._hypothesizer, HybridHypothesizer)  # type: ignore[attr-defined]
    assert isinstance(agents.diagnostician._diagnoser, HybridDiagnoser)  # type: ignore[attr-defined]
    assert isinstance(agents.fixer._strategy, HybridFixerStrategy)  # type: ignore[attr-defined]


def test_profile_llm_wires_claude_strategies_everywhere() -> None:
    """profile=llm -> ClaudeHypothesizer / ClaudeDiagnoser / ClaudeFixerStrategy."""
    from agents.diagnostician.diagnoser import ClaudeDiagnoser
    from agents.fixer.strategy import ClaudeFixerStrategy
    from agents.tester.hypothesizer import ClaudeHypothesizer

    agents = build_real_agents(AgentConfig(), profile="llm")
    assert isinstance(agents.tester._hypothesizer, ClaudeHypothesizer)  # type: ignore[attr-defined]
    assert isinstance(agents.diagnostician._diagnoser, ClaudeDiagnoser)  # type: ignore[attr-defined]
    assert isinstance(agents.fixer._strategy, ClaudeFixerStrategy)  # type: ignore[attr-defined]


def test_profile_default_is_static() -> None:
    """Default profile should be the safe / free one — no surprise LLM bills."""
    from agents.tester.hypothesizer import StaticHypothesizer

    agents = build_real_agents(AgentConfig())  # no profile=
    assert isinstance(agents.tester._hypothesizer, StaticHypothesizer)  # type: ignore[attr-defined]


def test_invalid_profile_raises() -> None:
    """Typos at the boundary should fail loud, not silently fall back."""
    import pytest as _pytest  # local alias

    from agents._factory import AgentConfigError

    with _pytest.raises(AgentConfigError, match="profile"):
        build_real_agents(AgentConfig(), profile="not-a-profile")  # type: ignore[arg-type]


def test_explicit_overrides_beat_profile() -> None:
    """An explicit hypothesizer / diagnoser / strategy override wins over profile selection."""
    from agents.diagnostician.diagnoser import FixtureDiagnoser as _FD
    from agents.fixer.strategy import FixtureFixerStrategy as _FFS
    from agents.tester.hypothesizer import FixtureHypothesizer as _FH

    fh = _FH([])
    fd = _FD([])
    ffs = _FFS(FixerOutput(reasoning="x", files_touched=[]))
    agents = build_real_agents(
        AgentConfig(),
        profile="hybrid",  # would normally produce Hybrid* — overrides win
        hypothesizer=fh,
        diagnoser=fd,
        fixer_strategy=ffs,
    )
    assert agents.tester._hypothesizer is fh  # type: ignore[attr-defined]
    assert agents.diagnostician._diagnoser is fd  # type: ignore[attr-defined]
    assert agents.fixer._strategy is ffs  # type: ignore[attr-defined]


def test_model_and_api_base_flow_through_to_llm_strategies() -> None:
    """profile=llm picks up model + api_base from config and threads them
    into the constructed Claude* strategies."""
    from agents.diagnostician.diagnoser import ClaudeDiagnoser
    from agents.fixer.strategy import ClaudeFixerStrategy
    from agents.tester.hypothesizer import ClaudeHypothesizer

    cfg = AgentConfig(model="ollama/qwen2.5-coder:14b", api_base="http://localhost:11434")
    agents = build_real_agents(cfg, profile="llm")
    h = agents.tester._hypothesizer  # type: ignore[attr-defined]
    assert isinstance(h, ClaudeHypothesizer)
    assert h.model == "ollama/qwen2.5-coder:14b"
    assert h.api_base == "http://localhost:11434"

    d = agents.diagnostician._diagnoser  # type: ignore[attr-defined]
    assert isinstance(d, ClaudeDiagnoser)
    assert d.model == "ollama/qwen2.5-coder:14b"
    assert d.api_base == "http://localhost:11434"

    fs = agents.fixer._strategy  # type: ignore[attr-defined]
    assert isinstance(fs, ClaudeFixerStrategy)
    assert fs.model == "ollama/qwen2.5-coder:14b"
    assert fs.api_base == "http://localhost:11434"


def test_agent_config_from_env_picks_up_model_and_api_base(monkeypatch) -> None:
    monkeypatch.setenv("CHAOS_LLM_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("CHAOS_LLM_API_BASE", "https://api.example/")
    cfg = AgentConfig.from_env()
    assert cfg.model == "openai/gpt-4o"
    assert cfg.api_base == "https://api.example/"


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
