"""Integration tests for HardwareChaosAgent against SimulatedHardwareIO."""

from __future__ import annotations

import asyncio

import pytest

from agents.chaos.hardware_agent import HardwareChaosAgent
from agents.chaos.hardware_io import HardwareFault, SimulatedHardwareIO
from shared.contracts import (
    ExperimentPlan,
    FaultSpec,
    SafetyConstraints,
)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _no_sleep(_secs: float) -> None:
    return None


def _plan(*, faults: list[FaultSpec], multi: bool = False) -> ExperimentPlan:
    return ExperimentPlan(
        title="hardware chaos test",
        target_app="neoowl",
        faults=faults,
        safety=SafetyConstraints(
            cluster_context="bench-hardware",
            namespace="bench",
            require_namespace_annotation=False,
            allow_multi_fault=multi,
        ),
    )


def _wifi_deauth_fault(duration: int = 10) -> FaultSpec:
    return FaultSpec(
        category="rf",  # type: ignore[arg-type]
        name="wifi.deauth",
        target_selector={"device": "dut-1"},
        parameters={"target_bssid": "auto", "intensity": "high"},
        duration_seconds=duration,
        requires_approval=False,
        rationale="test",
    )


# ---------------------------------------------------------------- happy path


def test_execute_happy_path_emits_scheduled_started_cleanup_events() -> None:
    sim = SimulatedHardwareIO()
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    plan = _plan(faults=[_wifi_deauth_fault()])

    timeline = _run(agent.execute(plan))

    assert timeline.success is True
    assert timeline.error is None
    events = [(e.fault_name, e.event) for e in timeline.events]
    assert events == [
        ("wifi.deauth", "scheduled"),
        ("wifi.deauth", "started"),
        ("wifi.deauth", "cleaned-up"),
    ]
    # All handles cleaned up — no lingering fault.
    assert len(sim._active_faults) == 0


def test_execute_started_event_carries_hardware_handle_id() -> None:
    sim = SimulatedHardwareIO()
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    timeline = _run(agent.execute(_plan(faults=[_wifi_deauth_fault()])))
    started = next(e for e in timeline.events if e.event == "started")
    assert started.detail.startswith("hardware/sim-inject-")


# ---------------------------------------------------------------- guards


def test_execute_without_hardware_fails_cleanly() -> None:
    agent = HardwareChaosAgent(hardware=None, sleep_fn=_no_sleep)
    timeline = _run(agent.execute(_plan(faults=[_wifi_deauth_fault()])))
    assert timeline.success is False
    assert "no hardware backend" in (timeline.error or "")


def test_execute_rejects_unknown_fault() -> None:
    sim = SimulatedHardwareIO()
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    unknown = FaultSpec(
        category="rf",  # type: ignore[arg-type]
        name="rf.unknown_attack",  # not in CATALOGUE
        target_selector={},
        parameters={},
        duration_seconds=5,
        requires_approval=False,
        rationale="x",
    )
    timeline = _run(agent.execute(_plan(faults=[unknown])))
    assert timeline.success is False
    assert "not in catalogue" in (timeline.error or "")


def test_execute_rejects_kubernetes_fault_on_hardware_path() -> None:
    """`network.loss` is in the catalogue (cloud path) but has no RF
    renderer — Phase 1's hardware agent must refuse it rather than try."""
    sim = SimulatedHardwareIO()
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    cloud_fault = FaultSpec(
        category="network",  # type: ignore[arg-type]
        name="network.loss",
        target_selector={},
        parameters={},
        duration_seconds=5,
        requires_approval=False,
        rationale="x",
    )
    timeline = _run(agent.execute(_plan(faults=[cloud_fault])))
    assert timeline.success is False
    assert "no hardware renderer" in (timeline.error or "")


def test_execute_rejects_multi_fault_without_opt_in() -> None:
    sim = SimulatedHardwareIO()
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    timeline = _run(
        agent.execute(
            _plan(faults=[_wifi_deauth_fault(), _wifi_deauth_fault()], multi=False)
        )
    )
    assert timeline.success is False
    assert "multiple faults" in (timeline.error or "")


# ---------------------------------------------------------------- exception path


def test_execute_cleans_up_handle_on_inject_failure() -> None:
    """If inject_fault raises mid-loop, every handle the agent opened so far
    must be cleaned up. SimulatedHardwareIO records active handles; we
    verify none leaked."""

    async def failing_hook(_fault: HardwareFault) -> None:
        raise RuntimeError("attack device unreachable")

    sim = SimulatedHardwareIO(inject_hook=failing_hook)
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    timeline = _run(agent.execute(_plan(faults=[_wifi_deauth_fault()])))

    assert timeline.success is False
    assert "attack device unreachable" in (timeline.error or "")
    # No handle should be left active.
    assert len(sim._active_faults) == 0


# ---------------------------------------------------------------- cleanup hook


@pytest.mark.asyncio
async def test_cleanup_resets_the_device() -> None:
    """orchestrator.cleanup_chaos should reset the DUT to a known state."""
    sim = SimulatedHardwareIO()
    fault = HardwareFault(name="wifi.deauth", parameters={}, duration_seconds=1)
    await sim.inject_fault(fault)  # leave a fault active before cleanup

    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    await agent.cleanup(_plan(faults=[_wifi_deauth_fault()]))
    assert len(sim._active_faults) == 0
    boot = await sim.read_telemetry("boot_count")
    assert boot.value == 1.0  # reset() incremented it
