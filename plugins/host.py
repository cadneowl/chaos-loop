"""
The plugin lifecycle host.

``PluginSession`` is an async context manager that runs the env/test *setup*
hooks on enter and **guarantees** the teardown hooks (plus any ``ctx.defer``
compensations) run on exit — success, assertion failure, crash, or abort. The
orchestrator opens a session around its run body and calls
``capture_baseline`` / ``drive_run`` / ``verify`` / ``collect_diagnostics`` at
the right points.

Guarantee model
---------------
Two unwind stacks: ``_env_stack`` (entered once) wraps ``_test_stack``. Each
teardown hook is registered the moment its scope opens — *before* the
corresponding setup hook runs — so a hook that throws mid-setup is still
unwound. If ``__aenter__`` fails partway, it unwinds what it registered and
re-raises (``__aexit__`` is not called for a failed enter, so enter cleans up
after itself). Teardown errors are logged and recorded, never raised — they
must not mask the primary failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack
from typing import Any, TypeVar

from plugins.base import (
    Compensation,
    ExperimentPlugin,
    PluginContext,
    SteadyStateGuard,
    overrides,
    stage_for,
)
from shared.contracts import (
    ExperimentPlan,
    LifecycleStage,
    StageResult,
    StageStatus,
    StatisticalSample,
    VerifyResult,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# Hooks run during setup, in order, with the scope they belong to.
_ENV_SETUP = ("provision_env", "await_ready", "seed")
_TEST_SETUP = ("setup_test",)


class GuardTripped(Exception):
    """Raised when a steady-state guard reports an unhealthy invariant."""

    def __init__(self, guard_name: str, detail: str) -> None:
        super().__init__(f"steady-state guard {guard_name!r} tripped: {detail}")
        self.guard_name = guard_name
        self.detail = detail


class Session:
    """Common interface so the orchestrator treats plugin / no-plugin uniformly."""

    records: list[StageResult]
    verify_result: VerifyResult | None
    diagnostics: dict[str, Any]
    plugin_name: str | None

    async def __aenter__(self) -> Session:  # pragma: no cover - overridden
        raise NotImplementedError

    async def __aexit__(self, *exc: object) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def capture_baseline(self) -> list[StatisticalSample]:
        raise NotImplementedError

    async def drive_run(self, inject: Callable[[], Coroutine[Any, Any, T]]) -> T:
        raise NotImplementedError

    async def verify(self) -> VerifyResult | None:
        raise NotImplementedError

    async def collect_diagnostics(self) -> dict[str, Any]:
        raise NotImplementedError


class NullSession(Session):
    """No-op session used when ``plan.plugin`` is None. Zero behavior change."""

    def __init__(self) -> None:
        self.records = []
        self.verify_result = None
        self.diagnostics = {}
        self.plugin_name = None

    async def __aenter__(self) -> NullSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def capture_baseline(self) -> list[StatisticalSample]:
        return []

    async def drive_run(self, inject: Callable[[], Coroutine[Any, Any, T]]) -> T:
        return await inject()

    async def verify(self) -> VerifyResult | None:
        return None

    async def collect_diagnostics(self) -> dict[str, Any]:
        return {}


class PluginSession(Session):
    """Runs a real plugin's lifecycle with guaranteed teardown."""

    def __init__(self, plugin: ExperimentPlugin, ctx: PluginContext) -> None:
        self.plugin = plugin
        self.ctx = ctx
        self.plugin_name = plugin.name
        self.records: list[StageResult] = []
        self.verify_result: VerifyResult | None = None
        self.diagnostics: dict[str, Any] = {}

        self._env_stack = AsyncExitStack()
        self._test_stack = AsyncExitStack()
        self._current = self._env_stack  # ctx.defer target; swapped per scope
        self._torn_down = False
        ctx._register = self._register

    # ----- ctx.defer plumbing ---------------------------------------------
    def _register(self, cleanup: Compensation, name: str) -> None:
        # Label the compensation by the scope it was registered in.
        stage = (
            LifecycleStage.TEARDOWN_TEST
            if self._current is self._test_stack
            else LifecycleStage.TEARDOWN_ENV
        )

        async def _wrapped() -> None:
            await self._run_teardown(stage, name, cleanup, takes_ctx=False)

        self._current.push_async_callback(_wrapped)

    # ----- async context manager ------------------------------------------
    async def __aenter__(self) -> PluginSession:
        try:
            await self._invoke("validate")
            # --- env scope ---
            self._current = self._env_stack
            if overrides(self.plugin, "teardown_env"):
                self._env_stack.push_async_callback(self._teardown_env)
            for hook in _ENV_SETUP:
                await self._invoke(hook)
            # --- test scope ---
            self._current = self._test_stack
            if overrides(self.plugin, "teardown_test"):
                self._test_stack.push_async_callback(self._teardown_test)
            for hook in _TEST_SETUP:
                await self._invoke(hook)
        except BaseException:
            # Enter failed partway; __aexit__ won't run, so unwind here.
            await self._teardown()
            raise
        return self

    async def __aexit__(self, *exc: object) -> bool:
        await self._teardown()
        return False  # never suppress the body's exception

    async def _teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        # Test scope first (inner), then env scope (outer). aclose runs the
        # registered callbacks in reverse registration order.
        for stack in (self._test_stack, self._env_stack):
            try:
                await stack.aclose()
            except Exception as e:  # pragma: no cover - defensive
                log.error("teardown stack raised: %r", e)

    async def _teardown_test(self) -> None:
        await self._run_teardown(
            LifecycleStage.TEARDOWN_TEST,
            "teardown_test",
            self.plugin.teardown_test,
            takes_ctx=True,
        )

    async def _teardown_env(self) -> None:
        await self._run_teardown(
            LifecycleStage.TEARDOWN_ENV,
            "teardown_env",
            self.plugin.teardown_env,
            takes_ctx=True,
        )

    # ----- mid-run hooks the orchestrator calls ---------------------------
    async def capture_baseline(self) -> list[StatisticalSample]:
        if not overrides(self.plugin, "capture_baseline"):
            self._skip("capture_baseline")
            return []
        samples = await self._invoke("capture_baseline")
        result: list[StatisticalSample] = samples or []
        self.ctx.baseline = result
        return result

    async def drive_run(self, inject: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Run the orchestrator's injection, concurrently driving the workload
        and polling the steady-state guard.

        Returns the injection's result. Raises ``GuardTripped`` if the guard
        trips (the orchestrator routes that to an abort). The guard and the
        workload driver are always cancelled before returning.
        """
        guard = (
            self.plugin.steady_state_guard(self.ctx)
            if overrides(self.plugin, "steady_state_guard")
            else None
        )
        drives_workload = overrides(self.plugin, "run_test")
        if guard is None and not drives_workload:
            return await inject()

        inject_task: asyncio.Task[T] = asyncio.create_task(inject(), name="inject")
        work: set[asyncio.Task[Any]] = {inject_task}
        if drives_workload:
            work.add(asyncio.create_task(self._invoke("run_test"), name="run_test"))
        guard_task = (
            asyncio.create_task(self._poll_guard(guard), name="guard")
            if guard is not None
            else None
        )

        pending: set[asyncio.Task[Any]] = set(work) | (
            {guard_task} if guard_task else set()
        )
        try:
            while not all(t.done() for t in work):
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    if t.exception() is not None:
                        raise t.exception()  # type: ignore[misc]
            return inject_task.result()
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _poll_guard(self, guard: SteadyStateGuard) -> None:
        while True:
            sample = await guard.check(self.ctx)
            if not sample.healthy:
                raise GuardTripped(guard.name, sample.detail)
            await asyncio.sleep(guard.interval_s)

    async def verify(self) -> VerifyResult | None:
        if not overrides(self.plugin, "verify"):
            self._skip("verify")
            return None
        result: VerifyResult | None = await self._invoke("verify")
        self.verify_result = result
        return result

    async def collect_diagnostics(self) -> dict[str, Any]:
        if not overrides(self.plugin, "collect_diagnostics"):
            self._skip("collect_diagnostics")
            return {}
        diag: dict[str, Any] = await self._invoke("collect_diagnostics") or {}
        self.diagnostics = diag
        return diag

    # ----- invocation + recording -----------------------------------------
    async def _invoke(self, hook: str) -> Any:
        """Run a setup/mid hook, recording an OK/FAILED/SKIPPED StageResult.

        Re-raises on failure (setup failures must abort the run); the recorded
        FAILED result is kept in the audit trail either way.
        """
        if not overrides(self.plugin, hook):
            self._skip(hook)
            return None
        stage = stage_for(hook)
        rec = StageResult(stage=stage, status=StageStatus.OK)
        start = time.monotonic()
        try:
            result = await getattr(self.plugin, hook)(self.ctx)
            rec.detail = "ok"
            return result
        except Exception as e:
            rec.status = StageStatus.FAILED
            rec.error = repr(e)
            log.warning("plugin %s.%s failed: %r", self.plugin.name, hook, e)
            raise
        finally:
            rec.duration_ms = (time.monotonic() - start) * 1000.0
            rec.finished_at = _now()
            self.records.append(rec)

    async def _run_teardown(
        self,
        stage: LifecycleStage,
        name: str,
        fn: Callable[..., Awaitable[None]],
        *,
        takes_ctx: bool,
    ) -> None:
        """Run one teardown/compensation best-effort: errors recorded, not raised.

        ``takes_ctx`` distinguishes the symmetric hooks (``teardown_test`` /
        ``teardown_env``, which receive ``ctx``) from ``ctx.defer``
        compensations (zero-arg callables).
        """
        rec = StageResult(stage=stage, status=StageStatus.OK, detail=name)
        start = time.monotonic()
        try:
            if takes_ctx:
                await fn(self.ctx)
            else:
                await fn()
        except Exception as e:
            rec.status = StageStatus.FAILED
            rec.error = repr(e)
            log.error("teardown %s (%s) failed: %r", stage.value, name, e)
        finally:
            rec.duration_ms = (time.monotonic() - start) * 1000.0
            rec.finished_at = _now()
            self.records.append(rec)

    def _skip(self, hook: str) -> None:
        self.records.append(StageResult(stage=stage_for(hook), status=StageStatus.SKIPPED))


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)


def open_session(plan: ExperimentPlan, plugin: ExperimentPlugin | None) -> Session:
    """Build the session for a run. Returns a no-op ``NullSession`` when there's
    no plugin, so the orchestrator's call sites stay unconditional."""
    if plugin is None:
        return NullSession()
    ctx = PluginContext(
        experiment_id=plan.experiment_id,
        plan=plan,
        config=dict(plan.plugin_config),
        log=logging.getLogger(f"plugins.{plugin.name}"),
    )
    return PluginSession(plugin, ctx)
