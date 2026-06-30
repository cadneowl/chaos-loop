"""Host semantics: lifecycle order, guaranteed teardown, defer, guard, auditing."""

from __future__ import annotations

import asyncio

import pytest

from plugins.base import (
    ExperimentPlugin,
    GuardSample,
    PluginContext,
    SteadyStateGuard,
)
from plugins.host import GuardTripped, NullSession, PluginSession, open_session
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    LifecycleStage,
    SafetyConstraints,
    StageStatus,
    VerifyResult,
)


def _plan() -> ExperimentPlan:
    return ExperimentPlan(
        title="host-test",
        target_app="demo",
        plugin="recording",
        faults=[
            FaultSpec(
                category=FaultCategory.NETWORK,
                name="network.loss",
                target_selector={"app": "demo"},
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


class RecordingPlugin(ExperimentPlugin):
    """Logs every hook it runs into ``self.events``; can be told to fail one."""

    name = "recording"

    def __init__(self, fail_at: str | None = None) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at

    async def _step(self, name: str) -> None:
        self.events.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"boom in {name}")

    async def validate(self, ctx: PluginContext) -> None:
        await self._step("validate")

    async def provision_env(self, ctx: PluginContext) -> None:
        await self._step("provision_env")

    async def await_ready(self, ctx: PluginContext) -> None:
        await self._step("await_ready")

    async def seed(self, ctx: PluginContext) -> None:
        await self._step("seed")

    async def setup_test(self, ctx: PluginContext) -> None:
        await self._step("setup_test")

    async def verify(self, ctx: PluginContext) -> VerifyResult:
        await self._step("verify")
        return VerifyResult(passed=True, summary="ok")

    async def teardown_test(self, ctx: PluginContext) -> None:
        await self._step("teardown_test")

    async def teardown_env(self, ctx: PluginContext) -> None:
        await self._step("teardown_env")


async def test_happy_path_runs_hooks_in_order() -> None:
    plugin = RecordingPlugin()
    async with open_session(_plan(), plugin) as session:
        await session.verify()
    assert plugin.events == [
        "validate",
        "provision_env",
        "await_ready",
        "seed",
        "setup_test",
        "verify",
        "teardown_test",
        "teardown_env",
    ]


async def test_teardown_runs_even_when_setup_fails() -> None:
    """A failure in seed still unwinds the env scope; the error propagates."""
    plugin = RecordingPlugin(fail_at="seed")
    with pytest.raises(RuntimeError, match="boom in seed"):
        async with open_session(_plan(), plugin):
            pass  # pragma: no cover - enter raises before body
    # seed failed before test scope opened: teardown_test never registered,
    # but teardown_env (registered at env-scope open) must still run.
    assert "teardown_env" in plugin.events
    assert "teardown_test" not in plugin.events
    assert plugin.events.index("teardown_env") > plugin.events.index("seed")


async def test_teardown_runs_when_body_raises() -> None:
    plugin = RecordingPlugin()
    with pytest.raises(ValueError, match="body"):
        async with open_session(_plan(), plugin):
            raise ValueError("body")
    assert plugin.events[-2:] == ["teardown_test", "teardown_env"]


async def test_defer_compensations_run_in_reverse() -> None:
    order: list[str] = []

    class DeferPlugin(ExperimentPlugin):
        name = "defer"

        async def seed(self, ctx: PluginContext) -> None:
            ctx.defer(lambda: _append(order, "first"), name="first")
            ctx.defer(lambda: _append(order, "second"), name="second")

    async def _append(target: list[str], val: str) -> None:
        target.append(val)

    async with open_session(_plan(), DeferPlugin()):
        pass
    assert order == ["second", "first"]


async def test_guard_trip_raises_and_cancels_work() -> None:
    cancelled = {"inject": False}

    class GuardPlugin(ExperimentPlugin):
        name = "guard"

        def steady_state_guard(self, ctx: PluginContext) -> SteadyStateGuard:
            async def _check(c: PluginContext) -> GuardSample:
                return GuardSample(healthy=False, detail="unhealthy")

            return SteadyStateGuard(name="g", check=_check, interval_s=0.001)

    async def _inject() -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["inject"] = True
            raise
        return "done"  # pragma: no cover

    async with open_session(_plan(), GuardPlugin()) as session:
        with pytest.raises(GuardTripped):
            await session.drive_run(_inject)
    assert cancelled["inject"] is True


async def test_skipped_hooks_recorded() -> None:
    """A plugin overriding only some hooks records SKIPPED for the rest."""

    class SparsePlugin(ExperimentPlugin):
        name = "sparse"

        async def provision_env(self, ctx: PluginContext) -> None:
            pass

    session = open_session(_plan(), SparsePlugin())
    assert isinstance(session, PluginSession)
    async with session:
        await session.capture_baseline()
        await session.verify()

    by_stage = {r.stage: r.status for r in session.records}
    assert by_stage[LifecycleStage.PROVISION_ENV] == StageStatus.OK
    assert by_stage[LifecycleStage.VALIDATE] == StageStatus.SKIPPED
    assert by_stage[LifecycleStage.CAPTURE_BASELINE] == StageStatus.SKIPPED
    assert by_stage[LifecycleStage.VERIFY] == StageStatus.SKIPPED


async def test_failed_setup_records_failed_stage() -> None:
    plugin = RecordingPlugin(fail_at="setup_test")
    session = open_session(_plan(), plugin)
    assert isinstance(session, PluginSession)
    with pytest.raises(RuntimeError):
        async with session:
            pass  # pragma: no cover
    failed = [r for r in session.records if r.status == StageStatus.FAILED]
    assert any(r.stage == LifecycleStage.SETUP_TEST for r in failed)
    assert failed[0].error is not None


async def test_null_session_is_noop() -> None:
    session = open_session(_plan(), None)
    assert isinstance(session, NullSession)
    async with session:
        assert await session.verify() is None
        assert await session.capture_baseline() == []
        assert await session.drive_run(_marker) == "marker"
    assert session.records == []


async def _marker() -> str:
    return "marker"


# --------------------------------------------------------------------------- #
# Teardown robustness                                                         #
# --------------------------------------------------------------------------- #


async def test_teardown_failure_recorded_but_not_raised() -> None:
    """A throwing teardown is recorded FAILED and does not mask success, and
    the *other* teardown still runs."""
    ran = {"test": False}

    class FailingTeardown(ExperimentPlugin):
        name = "fail-teardown"

        async def provision_env(self, ctx: PluginContext) -> None:
            ctx.env["x"] = 1

        async def teardown_test(self, ctx: PluginContext) -> None:
            ran["test"] = True

        async def teardown_env(self, ctx: PluginContext) -> None:
            raise RuntimeError("teardown boom")

    session = open_session(_plan(), FailingTeardown())
    assert isinstance(session, PluginSession)
    # No exception escapes even though teardown_env raises.
    async with session:
        pass
    assert ran["test"] is True
    env_td = [r for r in session.records if r.stage == LifecycleStage.TEARDOWN_ENV]
    assert env_td and env_td[0].status == StageStatus.FAILED
    assert "teardown boom" in (env_td[0].error or "")


async def test_defer_across_scopes_unwinds_test_then_env() -> None:
    order: list[str] = []

    async def _rec(label: str) -> None:
        order.append(label)

    class Scoped(ExperimentPlugin):
        name = "scoped"

        async def seed(self, ctx: PluginContext) -> None:  # env scope
            ctx.defer(lambda: _rec("env-comp"), name="env-comp")

        async def setup_test(self, ctx: PluginContext) -> None:  # test scope
            ctx.defer(lambda: _rec("test-comp"), name="test-comp")

    async with open_session(_plan(), Scoped()):
        pass
    assert order == ["test-comp", "env-comp"]


async def test_multiple_teardown_errors_all_recorded() -> None:
    class TwoFailures(ExperimentPlugin):
        name = "two-fail"

        async def seed(self, ctx: PluginContext) -> None:
            async def _boom() -> None:
                raise RuntimeError("defer boom")

            ctx.defer(_boom, name="boomer")

        async def teardown_env(self, ctx: PluginContext) -> None:
            raise RuntimeError("env boom")

    session = open_session(_plan(), TwoFailures())
    assert isinstance(session, PluginSession)
    async with session:
        pass
    failed = [r for r in session.records if r.status == StageStatus.FAILED]
    assert len(failed) == 2


# --------------------------------------------------------------------------- #
# drive_run / guard                                                           #
# --------------------------------------------------------------------------- #


async def test_guard_healthy_then_trips() -> None:
    class LateGuard(ExperimentPlugin):
        name = "late-guard"

        def steady_state_guard(self, ctx: PluginContext) -> SteadyStateGuard:
            counter = {"n": 0}

            async def _check(c: PluginContext) -> GuardSample:
                counter["n"] += 1
                return GuardSample(healthy=counter["n"] < 3, detail=f"poll {counter['n']}")

            return SteadyStateGuard(name="late", check=_check, interval_s=0.001)

    async def _inject() -> str:
        await asyncio.sleep(0.2)
        return "done"  # pragma: no cover

    async with open_session(_plan(), LateGuard()) as session:
        with pytest.raises(GuardTripped):
            await session.drive_run(_inject)


async def test_run_test_exception_propagates_and_cancels_inject() -> None:
    cancelled = {"inject": False}

    class BadWorkload(ExperimentPlugin):
        name = "bad-workload"

        async def run_test(self, ctx: PluginContext) -> None:
            raise RuntimeError("workload boom")

    async def _inject() -> str:
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            cancelled["inject"] = True
            raise
        return "done"  # pragma: no cover

    async with open_session(_plan(), BadWorkload()) as session:
        with pytest.raises(RuntimeError, match="workload boom"):
            await session.drive_run(_inject)
    assert cancelled["inject"] is True


async def test_drive_run_no_guard_no_workload_returns_inject() -> None:
    """A plugin overriding neither guard nor run_test: drive_run just injects."""

    class Plain(ExperimentPlugin):
        name = "plain"

        async def verify(self, ctx: PluginContext) -> VerifyResult:
            return VerifyResult(passed=True)

    async def _inject() -> int:
        return 7

    async with open_session(_plan(), Plain()) as session:
        assert await session.drive_run(_inject) == 7


async def test_drive_run_waits_for_workload_without_guard() -> None:
    ran = {"workload": False}

    class Workload(ExperimentPlugin):
        name = "workload"

        async def run_test(self, ctx: PluginContext) -> None:
            await asyncio.sleep(0.01)
            ran["workload"] = True

    async def _inject() -> int:
        return 42

    async with open_session(_plan(), Workload()) as session:
        result = await session.drive_run(_inject)
    assert result == 42
    assert ran["workload"] is True


# --------------------------------------------------------------------------- #
# mid-run hook plumbing                                                       #
# --------------------------------------------------------------------------- #


async def test_capture_baseline_stored_on_ctx() -> None:
    from shared.contracts import StatisticalSample

    class WithBaseline(ExperimentPlugin):
        name = "with-baseline"

        async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
            return [StatisticalSample.from_samples("m", [1.0, 2.0, 3.0])]

    session = open_session(_plan(), WithBaseline())
    assert isinstance(session, PluginSession)
    async with session:
        out = await session.capture_baseline()
    assert len(out) == 1
    assert session.ctx.baseline == out


async def test_collect_diagnostics_stored() -> None:
    class WithDiag(ExperimentPlugin):
        name = "with-diag"

        async def collect_diagnostics(self, ctx: PluginContext) -> dict[str, object]:
            return {"evidence": 42}

    session = open_session(_plan(), WithDiag())
    assert isinstance(session, PluginSession)
    async with session:
        diag = await session.collect_diagnostics()
    assert diag == {"evidence": 42}
    assert session.diagnostics == {"evidence": 42}


async def test_verify_returns_none_when_not_overridden() -> None:
    class NoVerify(ExperimentPlugin):
        name = "no-verify"

    async with open_session(_plan(), NoVerify()) as session:
        assert await session.verify() is None


async def test_validate_failure_aborts_enter() -> None:
    class BadValidate(ExperimentPlugin):
        name = "bad-validate"

        async def validate(self, ctx: PluginContext) -> None:
            raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        async with open_session(_plan(), BadValidate()):
            pass  # pragma: no cover


async def test_ctx_defer_without_host_is_noop() -> None:
    """A bare context (no host wired) ignores defer instead of crashing."""
    plan = _plan()
    ctx = PluginContext(experiment_id=plan.experiment_id, plan=plan)

    async def _cleanup() -> None:  # pragma: no cover - must never run
        raise AssertionError("should not run")

    ctx.defer(_cleanup)  # no _register wired -> silently ignored
