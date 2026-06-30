"""The web-service deployment plugin, unit + through the orchestrator loop.

Covers the realistic deployment lifecycle: provision -> ready -> seed -> run ->
verify -> teardown, with leak checks (the deployment is actually deleted), the
SLO-failure path, and a guard-trip abort.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.loop import Agents, ExperimentRunner
from orchestrator.store import ExperimentStore
from plugins.examples._fakes import DeploymentNotReady, FakeCluster
from plugins.examples.web_service_scenario import WebServiceScenario
from plugins.host import open_session
from shared.contracts import (
    AbortReason,
    ChaosTimeline,
    DiagnosisReport,
    DiagnosisRequest,
    ExperimentPlan,
    ExperimentState,
    FaultCategory,
    FaultSpec,
    FixAction,
    FixProposal,
    RootCauseHypothesis,
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


# --- steady fake agents so the *plugin* owns the verdict ---------------------


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
        return DiagnosisReport(
            experiment_id=req.experiment_id,
            hypotheses=[RootCauseHypothesis(summary="x", confidence=0.5, suggested_fix_class="test-gap")],
        )


class _FakeFixer:
    async def propose_fix(self, diagnosis: DiagnosisReport) -> FixProposal:
        return FixProposal(
            experiment_id=diagnosis.experiment_id,
            action=FixAction.NONE,
            confidence=0.0,
            reasoning="noop",
        )


class _CapturingWebService(WebServiceScenario):
    """Captures the FakeCluster so tests can assert the deployment was deleted."""

    name = "capturing-web-service"

    def __init__(self) -> None:
        self.captured: dict[str, FakeCluster] = {}

    async def provision_env(self, ctx: object) -> None:  # type: ignore[override]
        await super().provision_env(ctx)  # type: ignore[arg-type]
        self.captured["cluster"] = ctx.env["cluster"]  # type: ignore[attr-defined]


def _plan(**plugin_config: object) -> ExperimentPlan:
    return ExperimentPlan(
        title="web-service",
        target_app="web-service",
        plugin="example-web-service",
        plugin_config=dict(plugin_config),
        faults=[
            FaultSpec(
                category=FaultCategory.POD,
                name="pod.kill",
                target_selector={"app": "web-service"},
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


# --------------------------------------------------------------------------- #
# Through the loop                                                            #
# --------------------------------------------------------------------------- #


def test_web_service_healthy_run(store: ExperimentStore) -> None:
    diag = _SpyDiagnostician()
    plugin = _CapturingWebService()
    runner = ExperimentRunner(agents=_agents(diag=diag), store=store, plugin=plugin)
    record = asyncio.run(runner.run(_plan(simulate_degradation=False)))

    assert record.state == ExperimentState.RECORDED
    assert record.verify_result is not None and record.verify_result.passed
    assert diag.calls == 0
    # Deployment was actually deleted (no leak).
    assert plugin.captured["cluster"].names() == []
    stages = {(r.stage.value, r.status) for r in record.plugin_stage_results}
    assert ("await_ready", StageStatus.OK) in stages
    assert ("teardown_env", StageStatus.OK) in stages


def test_web_service_degraded_fails_slo(store: ExperimentStore) -> None:
    diag = _SpyDiagnostician()
    plugin = _CapturingWebService()
    runner = ExperimentRunner(agents=_agents(diag=diag), store=store, plugin=plugin)
    record = asyncio.run(
        runner.run(_plan(simulate_degradation=True, slo_error_rate=0.01, slo_p95_ms=100))
    )

    assert record.verify_result is not None
    assert record.verify_result.passed is False
    assert len(record.verify_result.failures) >= 1
    # Regression detected by the plugin only -> generic diagnostician skipped.
    assert diag.calls == 0
    assert record.diagnosis is None
    assert record.plugin_diagnostics.get("error_count", 0) > 0
    assert record.state == ExperimentState.RECORDED
    # Still no leak.
    assert plugin.captured["cluster"].names() == []


def test_web_service_guard_trip_aborts_and_cleans_up(store: ExperimentStore) -> None:
    chaos = _FakeChaos(execute_delay=0.05)  # hold the fault window open
    plugin = _CapturingWebService()
    runner = ExperimentRunner(agents=_agents(chaos=chaos), store=store, plugin=plugin)
    record = asyncio.run(
        runner.run(_plan(simulate_degradation=True, guard_max_error_rate=0.2))
    )

    assert record.state == ExperimentState.ABORTED
    assert record.abort_reason == AbortReason.SLO_BREACH
    assert chaos.cleanup_calls == 1
    # Teardown still ran; no leak.
    assert plugin.captured["cluster"].names() == []
    stages = {(r.stage.value, r.status) for r in record.plugin_stage_results}
    assert ("teardown_env", StageStatus.OK) in stages


# --------------------------------------------------------------------------- #
# Plugin unit (direct, via the host)                                          #
# --------------------------------------------------------------------------- #


async def test_plugin_unit_lifecycle_healthy() -> None:
    plugin = _CapturingWebService()
    async with open_session(_plan(simulate_degradation=False), plugin) as session:
        await session.capture_baseline()
        await session.drive_run(_noop_inject)
        vr = await session.verify()
    assert vr is not None and vr.passed
    # Deployment deleted on exit (leak check).
    assert plugin.captured["cluster"].names() == []


async def test_plugin_unit_lifecycle_degraded() -> None:
    plugin = _CapturingWebService()
    async with open_session(_plan(simulate_degradation=True, slo_p95_ms=100), plugin) as session:
        await session.capture_baseline()
        await session.drive_run(_noop_inject)
        vr = await session.verify()
        diag = await session.collect_diagnostics()
    assert vr is not None and not vr.passed
    assert diag["error_count"] > 0


async def test_await_ready_times_out_when_never_ready() -> None:
    cluster = FakeCluster(ready_after=100)  # needs 100 polls, wait caps at 10
    await cluster.apply("x", image="i", replicas=1)
    with pytest.raises(DeploymentNotReady):
        await cluster.wait_ready("x", max_polls=10)


async def _noop_inject() -> str:
    return "ok"
