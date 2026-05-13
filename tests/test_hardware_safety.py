"""Tests for the 5 hardware safety gates + their integration with the agent."""

from __future__ import annotations

import asyncio

from agents.chaos.hardware_agent import HardwareChaosAgent
from agents.chaos.hardware_io import SimulatedHardwareIO
from agents.chaos.hardware_safety import (
    HardwareSafetyConfig,
    check_battery_headroom,
    check_bench_mode,
    check_emission_compliance,
    check_geofence,
    check_thermal_headroom,
    run_all_gates,
)
from shared.contracts import (
    AbortReason,
    ExperimentPlan,
    FaultSpec,
    SafetyConstraints,
)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _no_sleep(_secs: float) -> None:
    return None


def _wifi_fault(channel: int = 6) -> FaultSpec:
    return FaultSpec(
        category="rf",  # type: ignore[arg-type]
        name="wifi.deauth",
        target_selector={"device": "dut-1"},
        parameters={"channel": channel, "intensity": "medium"},
        duration_seconds=10,
        requires_approval=False,
        rationale="test",
    )


def _plan(faults: list[FaultSpec] | None = None) -> ExperimentPlan:
    return ExperimentPlan(
        title="t",
        target_app="neoowl",
        faults=faults or [_wifi_fault()],
        safety=SafetyConstraints(
            cluster_context="bench-hardware",
            namespace="bench",
            require_namespace_annotation=False,
        ),
    )


# ---------------------------------------------------------------- bench-mode


def test_bench_mode_passes_when_dut_reports_bench() -> None:
    sim = SimulatedHardwareIO()  # default mode=BENCH
    cfg = HardwareSafetyConfig()
    assert _run(check_bench_mode(sim, cfg)) is None


def test_bench_mode_refuses_when_dut_is_production() -> None:
    from agents.chaos.hardware_io import DeviceInfo

    sim = SimulatedHardwareIO(
        device=DeviceInfo(
            serial="prod-DUT-001",
            firmware_version="2.0.0",
            hardware_revision="rev-C",
            mode="PRODUCTION",
        )
    )
    cfg = HardwareSafetyConfig()
    fail = _run(check_bench_mode(sim, cfg))
    assert fail is not None
    assert fail.reason == AbortReason.CLUSTER_DENIED
    assert "BENCH" in fail.detail


def test_bench_mode_refuses_when_serial_mismatches_expected() -> None:
    sim = SimulatedHardwareIO()  # serial sim-DUT-001
    cfg = HardwareSafetyConfig(expected_serial="OTHER-DUT-999")
    fail = _run(check_bench_mode(sim, cfg))
    assert fail is not None
    assert "wrong device" in fail.detail


# ---------------------------------------------------------------- geofence


def test_geofence_passes_when_disabled_by_default() -> None:
    sim = SimulatedHardwareIO()
    cfg = HardwareSafetyConfig()  # expected_geofence_tag = None
    assert _run(check_geofence(sim, cfg)) is None


def test_geofence_passes_when_tag_matches() -> None:
    sim = SimulatedHardwareIO(geofence_tag="lab-bench-12")
    cfg = HardwareSafetyConfig(expected_geofence_tag="lab-bench-12")
    assert _run(check_geofence(sim, cfg)) is None


def test_geofence_refuses_when_tag_drifts() -> None:
    sim = SimulatedHardwareIO(geofence_tag="kitchen-table")
    cfg = HardwareSafetyConfig(expected_geofence_tag="lab-bench-12")
    fail = _run(check_geofence(sim, cfg))
    assert fail is not None
    assert fail.reason == AbortReason.CLUSTER_DENIED
    assert "lab-bench-12" in fail.detail


# ---------------------------------------------------------------- thermal


def test_thermal_passes_at_default_temperature() -> None:
    sim = SimulatedHardwareIO()  # die_temperature_c = 35
    assert _run(check_thermal_headroom(sim, HardwareSafetyConfig())) is None


def test_thermal_refuses_when_die_temp_above_threshold() -> None:
    sim = SimulatedHardwareIO()
    sim.metric_overrides["die_temperature_c"] = 82.0
    fail = _run(check_thermal_headroom(sim, HardwareSafetyConfig()))
    assert fail is not None
    assert fail.reason == AbortReason.BLAST_RADIUS_VIOLATION
    assert "82.0" in fail.detail


# ---------------------------------------------------------------- battery


def test_battery_passes_when_charged() -> None:
    sim = SimulatedHardwareIO()  # battery_soc = 0.90
    assert _run(check_battery_headroom(sim, HardwareSafetyConfig())) is None


def test_battery_refuses_when_below_threshold() -> None:
    sim = SimulatedHardwareIO()
    sim.metric_overrides["battery_soc"] = 0.20
    fail = _run(check_battery_headroom(sim, HardwareSafetyConfig()))
    assert fail is not None
    assert fail.reason == AbortReason.BLAST_RADIUS_VIOLATION
    assert "0.20" in fail.detail


# ---------------------------------------------------------------- emission


def test_emission_passes_for_ism_channel_in_default_license() -> None:
    plan = _plan([_wifi_fault(channel=6)])
    assert check_emission_compliance(plan, HardwareSafetyConfig()) is None


def test_emission_refuses_channel_outside_license() -> None:
    """Default license covers ch 1-14 only; ch 36 (5 GHz U-NII) is out."""
    plan = _plan([_wifi_fault(channel=36)])
    fail = check_emission_compliance(plan, HardwareSafetyConfig())
    assert fail is not None
    assert fail.reason == AbortReason.CLUSTER_DENIED
    assert "channel 36" in fail.detail


def test_emission_allows_5ghz_when_license_includes_it() -> None:
    plan = _plan([_wifi_fault(channel=36)])
    cfg = HardwareSafetyConfig(licensed_bands=((1, 14), (36, 165)))
    assert check_emission_compliance(plan, cfg) is None


def test_emission_refuses_channel_sweep_without_ism_coverage() -> None:
    """Channel 0 means sweep across the band; only safe if the license
    covers the entire ISM 1-14 range."""
    plan = _plan([_wifi_fault(channel=0)])
    cfg = HardwareSafetyConfig(licensed_bands=((36, 165),))  # 5GHz only, no ISM
    fail = check_emission_compliance(plan, cfg)
    assert fail is not None
    assert "sweep" in fail.detail


def test_emission_allows_sweep_with_ism_license() -> None:
    plan = _plan([_wifi_fault(channel=0)])
    cfg = HardwareSafetyConfig(licensed_bands=((1, 14),))
    assert check_emission_compliance(plan, cfg) is None


# ---------------------------------------------------------------- runner


def test_run_all_gates_returns_first_failure() -> None:
    """If multiple gates would fail, the runner returns the first one so
    subsequent gates aren't second-guessed."""
    sim = SimulatedHardwareIO()
    sim.metric_overrides["die_temperature_c"] = 85.0  # would fail thermal
    sim.metric_overrides["battery_soc"] = 0.1  # would also fail battery
    plan = _plan([_wifi_fault(channel=36)])  # would also fail emission
    cfg = HardwareSafetyConfig()  # default license — refuses ch 36
    fail = _run(run_all_gates(plan, sim, cfg))
    assert fail is not None
    # Emission runs first in the runner — confirms ordering.
    assert "channel 36" in fail.detail


def test_run_all_gates_passes_clean_plan_against_healthy_dut() -> None:
    sim = SimulatedHardwareIO()
    plan = _plan([_wifi_fault(channel=6)])
    assert _run(run_all_gates(plan, sim, HardwareSafetyConfig())) is None


# ---------------------------------------------------------------- agent integration


def test_agent_aborts_when_safety_gate_fails() -> None:
    """The chaos agent's pre-flight must surface a hardware-safety failure
    as a `ChaosTimeline(success=False, error="hardware safety gate: ...")`
    so the orchestrator's existing abort path handles it."""
    sim = SimulatedHardwareIO()
    sim.metric_overrides["battery_soc"] = 0.1
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    timeline = _run(agent.execute(_plan()))
    assert timeline.success is False
    assert "hardware safety gate" in (timeline.error or "")
    assert "battery" in (timeline.error or "").lower()


def test_agent_passes_through_with_default_safety_config() -> None:
    """Default config + healthy simulator + valid plan: chaos runs normally."""
    sim = SimulatedHardwareIO()
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
    timeline = _run(agent.execute(_plan()))
    assert timeline.success is True
    assert timeline.error is None


def test_agent_honors_custom_safety_config() -> None:
    """Passing an explicit HardwareSafetyConfig changes pre-flight behavior."""
    sim = SimulatedHardwareIO(geofence_tag="moved-the-bench")
    cfg = HardwareSafetyConfig(expected_geofence_tag="lab-bench-12")
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep, safety_config=cfg)
    timeline = _run(agent.execute(_plan()))
    assert timeline.success is False
    assert "geofence" in (timeline.error or "").lower()
