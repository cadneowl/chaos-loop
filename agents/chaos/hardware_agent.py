"""
HardwareChaosAgent — chaos agent that drives a hardware bench instead of
the Kubernetes API.

Satisfies `orchestrator.loop.ChaosAgent` Protocol, so the orchestrator
plugs it in wherever it would plug in `ClaudeChaosAgent`. Same loop,
same audit trail, same pause/resume/abort semantics — just a different
bottom half.

Phase 1: this is wired to RF renderers only (`agents/chaos/faults/rf.py`).
Phase 3 will add power.* / sensor.* / time.* renderers and either
extend this agent or add sibling agents per category.

See docs/NEOOWL_ADAPTATION.md for the broader plan.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from agents.chaos.faults import _meta
from agents.chaos.faults.rf import has_rf_renderer, render_rf_fault
from agents.chaos.hardware_io import HardwareIO, InjectionHandle
from shared.contracts import ChaosTimeline, ExperimentPlan, TimelineEvent

log = logging.getLogger(__name__)

# Mirrors the Literal in shared/contracts.py:TimelineEvent.event.
_EventKind = Literal[
    "scheduled", "started", "verified-active", "stopped", "cleaned-up", "error"
]


def _event(fault_name: str, kind: _EventKind, detail: str = "") -> TimelineEvent:
    return TimelineEvent(
        timestamp=datetime.now(tz=UTC),
        fault_name=fault_name,
        event=kind,
        detail=detail,
    )


class HardwareChaosAgent:
    """Implements `orchestrator.loop.ChaosAgent` for hardware benches.

    The execute() loop mirrors ClaudeChaosAgent's: render → inject →
    sleep duration → cleanup, with the same scheduled/started/cleaned-up
    timeline events the UI already renders. Cleanup is unconditional on
    the exception path so a failed run leaves the DUT in a known state.
    """

    def __init__(
        self,
        *,
        hardware: HardwareIO | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._hardware = hardware
        self._sleep: Callable[[float], Awaitable[None]] = sleep_fn or asyncio.sleep

    async def execute(self, plan: ExperimentPlan) -> ChaosTimeline:
        """Render each fault to a HardwareFault, inject, wait, clean up.

        Same shape as ClaudeChaosAgent.execute() but bottomed at a
        `HardwareIO` instead of a `ClusterIO`. Quiet windows pre/post
        mirror the original so the tester gets clean baseline and
        post-injection windows.
        """
        if self._hardware is None:
            return ChaosTimeline(
                experiment_id=plan.experiment_id,
                events=[],
                success=False,
                error="no hardware backend configured",
            )

        # Pre-flight: catalogue + renderer + multi-fault gate (same checks the
        # kubernetes agent runs; reuses the shared catalogue but a separate
        # renderer registry).
        for fault in plan.faults:
            if fault.name not in _meta.CATALOGUE:
                return self._fail(plan, f"unknown fault {fault.name!r}; not in catalogue")
            if not has_rf_renderer(fault.name):
                return self._fail(
                    plan,
                    f"fault {fault.name!r} has no hardware renderer "
                    "(only RF faults are wired in Phase 1)",
                )
        if len(plan.faults) > 1 and not plan.safety.allow_multi_fault:
            return self._fail(plan, "plan has multiple faults but allow_multi_fault is False")

        events: list[TimelineEvent] = []
        # Track every handle we open so cleanup-on-exception is exhaustive.
        active_handles: list[InjectionHandle] = []

        try:
            if plan.quiet_window_pre_seconds > 0:
                await self._sleep(plan.quiet_window_pre_seconds)

            for fault in plan.faults:
                events.append(_event(fault.name, "scheduled"))
                rendered = render_rf_fault(fault)
                handle = await self._hardware.inject_fault(rendered)
                active_handles.append(handle)
                events.append(
                    _event(
                        fault.name,
                        "started",
                        f"hardware/{handle.id}",
                    )
                )

                await self._sleep(fault.duration_seconds)

                await self._hardware.cleanup(handle)
                active_handles.remove(handle)
                events.append(_event(fault.name, "cleaned-up", "ok"))

            if plan.quiet_window_post_seconds > 0:
                await self._sleep(plan.quiet_window_post_seconds)

            return ChaosTimeline(
                experiment_id=plan.experiment_id,
                events=events,
                success=True,
            )
        except Exception as e:
            # Best-effort cleanup of any handle that didn't reach its own
            # cleanup call site. Idempotent on the HardwareIO contract.
            for handle in active_handles:
                try:
                    await self._hardware.cleanup(handle)
                    events.append(
                        _event("cleanup", "cleaned-up", f"forced cleanup of {handle.id}")
                    )
                except Exception as ce:
                    log.warning("hardware cleanup of %s failed: %s", handle.id, ce)
            return ChaosTimeline(
                experiment_id=plan.experiment_id,
                events=events,
                success=False,
                error=repr(e),
            )

    async def cleanup(self, plan: ExperimentPlan) -> None:
        """Orchestrator may call this on abort. Best-effort device reset."""
        if self._hardware is None:
            return
        try:
            await self._hardware.reset()
        except Exception as e:
            log.warning("hardware reset failed during cleanup: %s", e)

    async def get_namespace_annotations(self, namespace: str) -> dict[str, str] | None:
        """No-op for hardware. Returns an empty dict so the safety gate sees
        a present-but-empty annotation map (i.e., we explicitly opted out of
        the namespace-annotation check; bench-mode safety lives elsewhere)."""
        return {}

    # ---- helpers -------------------------------------------------------

    def _fail(self, plan: ExperimentPlan, msg: str) -> ChaosTimeline:
        return ChaosTimeline(
            experiment_id=plan.experiment_id,
            events=[],
            success=False,
            error=msg,
        )
