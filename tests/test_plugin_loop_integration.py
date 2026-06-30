"""The plugin lifecycle wired through the orchestrator loop end-to-end."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.loop import Agents, ExperimentRunner
from orchestrator.store import ExperimentStore
from plugins.base import GuardSample, PluginContext, SteadyStateGuard
from plugins.examples.keyvalue_scenario import KeyValueScenario
from shared.contracts import (
    AbortReason,
    ChaosTimeline,
    DiagnosisReport,
    DiagnosisRequest,
    ExperimentPlan,
    ExperimentState,
    FaultCategory,
    FaultSpec,
    FixProposal,
    SafetyConstraints,
    SecurityReport,
    SecurityRequest,
    StageStatus,
    TesterReport,
    TesterRequest,
    TimelineEvent,
)


@pytest.fixture
def store(tmp_path: Path) -> ExperimentStore:
    return ExperimentStore(tmp_path / "experiments.sqlite")


# --- fakes: steady tester/security so the *plugin* owns the verdict ----------


class _SteadyTester:
    async def baseline(self, req: TesterRequest) -> TesterReport:
        return TesterReport(request_kind="baseline", experiment_id=req.experiment_id, steady_state=True)

    async def verify(self, req: TesterRequest) -> TesterReport:
        return TesterReport(request_kind="verify", experiment_id=req.experiment_id, steady_state=True)


class _CleanSecurity:
    async def baseline(self, req: SecurityRequest) -> SecurityReport:
        return SecurityReport(request_kind="baseline", experiment_id=req.experiment_id)

    async def verify(self, req: SecurityRequest) -> SecurityReport:
        return SecurityReport(request_kind="verify", experiment_id=req.experiment_id)


class _FakeChaos:
    def __init__(self, execute_delay: float = 0.0) -> None:
        self.execute_delay = execute_delay
        self.cleanup_calls = 0

    async def execute(self, plan: ExperimentPlan) -> ChaosTimeline:
        if self.execute_delay:
            await asyncio.sleep(self.execute_delay)
        from datetime import UTC, datetime

        now = datetime.now(tz=UTC)
        return ChaosTimeline(
            experiment_id=plan.experiment_id,
            events=[TimelineEvent(timestamp=now, fault_name=plan.faults[0].name, event="started")],
            success=True,
        )

    async def cleanup(self, plan: ExperimentPlan) -> None:
        self.cleanup_calls += 1

    async def get_namespace_annotations(self, namespace: str) -> dict[str, str]:
        return {"chaos.kosta.dev/allowed": "true"}


class _SpyDiagnostician:
    def __init__(self) -> None:
        self.calls = 0

    async def diagnose(self, req: DiagnosisRequest) -> DiagnosisReport:
        self.calls += 1
        from shared.contracts import RootCauseHypothesis

        return DiagnosisReport(
            experiment_id=req.experiment_id,
            hypotheses=[
                RootCauseHypothesis(summary="x", confidence=0.5, suggested_fix_class="test-gap")
            ],
        )


class _FakeFixer:
    async def propose_fix(self, diagnosis: DiagnosisReport) -> FixProposal:
        from shared.contracts import FixAction

        return FixProposal(
            experiment_id=diagnosis.experiment_id,
            action=FixAction.NONE,
            confidence=0.0,
            reasoning="noop",
        )


def _plan(**plugin_config: object) -> ExperimentPlan:
    return ExperimentPlan(
        title="kv-plugin",
        target_app="keyvalue-demo",
        plugin="example-keyvalue",
        plugin_config=dict(plugin_config),
        faults=[
            FaultSpec(
                category=FaultCategory.NETWORK,
                name="network.loss",
                target_selector={"app": "keyvalue-demo"},
                duration_seconds=1,
                rationale="test",
            )
        ],
        safety=SafetyConstraints(
            cluster_context="kind-dev",
            namespace="demo",
            require_namespace_annotation=False,
        ),
    )


def _agents(chaos: _FakeChaos | None = None, diag: _SpyDiagnostician | None = None) -> Agents:
    return Agents(
        tester=_SteadyTester(),
        security=_CleanSecurity(),
        chaos=chaos or _FakeChaos(),
        diagnostician=diag or _SpyDiagnostician(),
        fixer=_FakeFixer(),
    )


def test_plugin_pass_reaches_steady(store: ExperimentStore) -> None:
    diag = _SpyDiagnostician()
    runner = ExperimentRunner(
        agents=_agents(diag=diag), store=store, plugin=KeyValueScenario()
    )
    record = asyncio.run(runner.run(_plan(inject_data_loss=False)))

    # STEADY is transient; a clean run finishes RECORDED with no diagnosis/fix.
    assert record.state == ExperimentState.RECORDED
    assert record.diagnosis is None
    assert record.fix_proposal is None
    assert record.verify_result is not None and record.verify_result.passed
    assert record.plugin_name == "example-keyvalue"
    assert diag.calls == 0  # no regression, no diagnosis
    # Teardown audit is persisted.
    stages = {(r.stage.value, r.status) for r in record.plugin_stage_results}
    assert ("teardown_env", StageStatus.OK) in stages
    assert ("teardown_test", StageStatus.OK) in stages


def test_plugin_failure_marks_regressed_without_builtin_diagnosis(store: ExperimentStore) -> None:
    diag = _SpyDiagnostician()
    runner = ExperimentRunner(
        agents=_agents(diag=diag), store=store, plugin=KeyValueScenario()
    )
    record = asyncio.run(
        runner.run(_plan(seed_keys=["a", "b", "c"], inject_data_loss=True))
    )

    # Plugin verify failed; tester/security are steady, so the generic
    # diagnostician is not invoked, but the failure is fully recorded.
    assert record.verify_result is not None
    assert record.verify_result.passed is False
    assert len(record.verify_result.failures) == 1
    assert record.diagnosis is None
    assert diag.calls == 0
    assert record.plugin_diagnostics.get("lost_key") == "a"
    assert record.state == ExperimentState.RECORDED


def test_plugin_teardown_runs_on_abort(store: ExperimentStore) -> None:
    """A guard trip aborts the run, cleans up the fault, and still tears down."""
    chaos = _FakeChaos(execute_delay=0.5)  # keep the fault window open

    class GuardedKV(KeyValueScenario):
        name = "guarded-kv"

        def steady_state_guard(self, ctx: PluginContext) -> SteadyStateGuard:
            async def _check(c: PluginContext) -> GuardSample:
                # Trip immediately.
                return GuardSample(healthy=False, detail="forced trip")

            return SteadyStateGuard(name="forced", check=_check, interval_s=0.001)

    runner = ExperimentRunner(agents=_agents(chaos=chaos), store=store, plugin=GuardedKV())
    record = asyncio.run(runner.run(_plan(inject_data_loss=False)))

    assert record.state == ExperimentState.ABORTED
    assert record.abort_reason == AbortReason.SLO_BREACH
    assert chaos.cleanup_calls == 1  # best-effort fault cleanup on guard trip
    stages = {(r.stage.value, r.status) for r in record.plugin_stage_results}
    assert ("teardown_env", StageStatus.OK) in stages


def test_provision_failure_aborts_gracefully(store: ExperimentStore) -> None:
    """A plugin whose provision_env raises yields an ABORTED record (not a
    crash), runs teardown, and persists the audit trail."""
    from plugins.base import ExperimentPlugin

    torn_down = {"env": False}

    class BrokenProvision(ExperimentPlugin):
        name = "broken-provision"

        async def provision_env(self, ctx) -> None:
            raise RuntimeError("kubectl apply exploded")

        async def teardown_env(self, ctx) -> None:
            torn_down["env"] = True

    runner = ExperimentRunner(agents=_agents(), store=store, plugin=BrokenProvision())
    record = asyncio.run(runner.run(_plan()))

    assert record.state == ExperimentState.ABORTED
    assert record.abort_reason == AbortReason.AGENT_FAILURE
    assert "kubectl apply exploded" in record.abort_detail
    # teardown_env was registered before provision ran, so it still fired.
    assert torn_down["env"] is True
    # The failed stage is in the persisted audit trail.
    loaded = store.load(record.experiment_id)
    assert loaded is not None
    failed = [r for r in loaded.plugin_stage_results if r.status == StageStatus.FAILED]
    assert any(r.stage.value == "provision_env" for r in failed)


def test_no_plugin_is_unchanged(store: ExperimentStore) -> None:
    """Without a plugin, the record carries no plugin audit and runs as before."""
    runner = ExperimentRunner(agents=_agents(), store=store)  # plugin=None
    record = asyncio.run(runner.run(_plan()))
    assert record.plugin_name is None
    assert record.plugin_stage_results == []
    assert record.verify_result is None
    assert record.state == ExperimentState.RECORDED
