"""Tests for the three new RF renderers + their catalogue entries."""

from __future__ import annotations

import asyncio

import pytest

from agents.chaos.faults._meta import CATALOGUE
from agents.chaos.faults.rf import (
    has_rf_renderer,
    render_ble_advertising_flood,
    render_lora_jam,
    render_rf_fault,
    render_wifi_jam,
)
from agents.chaos.hardware_agent import HardwareChaosAgent
from agents.chaos.hardware_io import HardwareFault, SimulatedHardwareIO
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    SafetyConstraints,
)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


async def _no_sleep(_secs: float) -> None:
    return None


def _spec(name: str, params: dict[str, object] | None = None, duration: int = 5) -> FaultSpec:
    return FaultSpec(
        category="rf",  # type: ignore[arg-type]
        name=name,
        target_selector={"device": "dut-1"},
        parameters=params or {},
        duration_seconds=duration,
        requires_approval=False,
        rationale="test",
    )


# ---------------------------------------------------------------- catalogue


def test_all_three_new_faults_registered_in_catalogue() -> None:
    for name in ("wifi.jam", "ble.advertising_flood", "lora.jam"):
        assert name in CATALOGUE, f"{name} missing from catalogue"
        entry = CATALOGUE[name]
        assert entry.category == FaultCategory.RF
        assert entry.chaos_mesh_kind is None, f"{name} should not have a CRD kind"


def test_emission_heavy_faults_require_approval() -> None:
    """`wifi.jam` and `lora.jam` are continuous-carrier emissions; they
    deserve the requires_approval=True surface so the safety/approval
    gate (existing or future) refuses to run them silently."""
    assert CATALOGUE["wifi.jam"].requires_approval is True
    assert CATALOGUE["lora.jam"].requires_approval is True
    # BLE flood is digital and within ISM; doesn't need extra approval.
    assert CATALOGUE["ble.advertising_flood"].requires_approval is False


def test_all_three_new_renderers_registered() -> None:
    for name in ("wifi.jam", "ble.advertising_flood", "lora.jam"):
        assert has_rf_renderer(name), f"no renderer for {name}"


# ---------------------------------------------------------------- wifi.jam


def test_wifi_jam_renders_with_defaults() -> None:
    out = render_wifi_jam(_spec("wifi.jam"))
    assert out.name == "wifi.jam"
    assert out.parameters == {
        "channel": 0,
        "power_dbm": 0,
        "sweep_period_ms": 100,
    }


def test_wifi_jam_honors_explicit_parameters() -> None:
    out = render_wifi_jam(
        _spec("wifi.jam", {"channel": 11, "power_dbm": 15, "sweep_period_ms": 50})
    )
    assert out.parameters == {"channel": 11, "power_dbm": 15, "sweep_period_ms": 50}


# ---------------------------------------------------------------- ble.advertising_flood


def test_ble_flood_renders_with_defaults() -> None:
    out = render_ble_advertising_flood(_spec("ble.advertising_flood"))
    assert out.parameters == {
        "rate_per_second": 10000,
        "spoofed_macs": 256,
        "adv_data_size": 28,
    }


def test_ble_flood_honors_explicit_parameters() -> None:
    out = render_ble_advertising_flood(
        _spec(
            "ble.advertising_flood",
            {"rate_per_second": 2000, "spoofed_macs": 16, "adv_data_size": 24},
        )
    )
    assert out.parameters == {
        "rate_per_second": 2000,
        "spoofed_macs": 16,
        "adv_data_size": 24,
    }


# ---------------------------------------------------------------- lora.jam


def test_lora_jam_renders_with_defaults() -> None:
    out = render_lora_jam(_spec("lora.jam"))
    assert out.parameters["frequency_hz"] == 915_000_000  # US default
    assert out.parameters["bandwidth_hz"] == 125_000
    assert out.parameters["spreading_factor"] == 7
    assert out.parameters["power_dbm"] == 14


def test_lora_jam_honors_eu868_override() -> None:
    """Operators outside the US set frequency_hz to their regional band."""
    out = render_lora_jam(_spec("lora.jam", {"frequency_hz": 868_000_000}))
    assert out.parameters["frequency_hz"] == 868_000_000


# ---------------------------------------------------------------- dispatch


def test_render_rf_fault_dispatches_to_correct_renderer() -> None:
    out = render_rf_fault(_spec("wifi.jam", {"channel": 6}))
    assert out.name == "wifi.jam"
    assert out.parameters["channel"] == 6


def test_render_rf_fault_raises_for_unknown_name() -> None:
    with pytest.raises(KeyError, match="no RF renderer"):
        render_rf_fault(_spec("rf.does_not_exist"))


# ---------------------------------------------------------------- simulator coupling


def test_simulator_wifi_jam_degrades_detector_latency() -> None:
    """Symmetric with wifi.deauth — both block WiFi traffic, so the
    detector latency probe should reflect either fault being active."""
    sim = SimulatedHardwareIO()
    fault = HardwareFault(name="wifi.jam", parameters={}, duration_seconds=10)
    handle = _run(sim.inject_fault(fault))
    sample = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample.value == sim._degraded_latency_ms
    _run(sim.cleanup(handle))


def test_simulator_ble_and_lora_dont_affect_wifi_detector() -> None:
    """BLE / LoRa faults exercise different subsystems; the WiFi detector
    latency should stay at baseline. (Phase 3 will surface BLE/LoRa-
    specific metrics that DO change under these faults.)"""
    sim = SimulatedHardwareIO()
    for name in ("ble.advertising_flood", "lora.jam"):
        h = _run(sim.inject_fault(HardwareFault(name=name, parameters={}, duration_seconds=10)))
        sample = _run(sim.read_telemetry("detector_latency_p95_ms"))
        assert sample.value == sim._baseline_latency_ms, f"{name} should not affect wifi detector"
        _run(sim.cleanup(h))


# ---------------------------------------------------------------- agent end-to-end


def test_agent_executes_each_new_fault_end_to_end() -> None:
    """Each new fault must survive the agent's full execute() loop —
    catalogue → renderer → simulator inject → cleanup → success timeline."""
    for name in ("wifi.jam", "ble.advertising_flood", "lora.jam"):
        sim = SimulatedHardwareIO()
        agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
        plan = ExperimentPlan(
            title="t",
            target_app="neoowl",
            faults=[_spec(name, duration=3)],
            safety=SafetyConstraints(
                cluster_context="bench-hardware",
                namespace="bench",
                require_namespace_annotation=False,
            ),
        )
        timeline = _run(agent.execute(plan))
        assert timeline.success is True, f"{name}: {timeline.error}"
        events = [(e.fault_name, e.event) for e in timeline.events]
        assert (name, "scheduled") in events
        assert (name, "started") in events
        assert (name, "cleaned-up") in events
        # And no leaks.
        assert len(sim._active_faults) == 0
