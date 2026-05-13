"""Tests for the Phase 3 power.* / sensor.* / time.* hardware faults."""

from __future__ import annotations

import asyncio

import pytest

from agents.chaos.faults._meta import CATALOGUE
from agents.chaos.faults.power import (
    has_power_renderer,
    render_power_brownout,
    render_power_cut,
    render_power_fault,
    render_power_ramp,
)
from agents.chaos.faults.sensor import (
    has_sensor_renderer,
    render_sensor_dropout,
    render_sensor_fault,
    render_sensor_stuck,
)
from agents.chaos.faults.time_hardware import (
    has_time_renderer,
    render_time_clock_drift,
    render_time_fault,
    render_time_ntp_cut,
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
    defn = CATALOGUE[name]
    return FaultSpec(
        category=defn.category,
        name=name,
        target_selector={"device": "dut-1"},
        parameters=params or {},
        duration_seconds=duration,
        requires_approval=False,
        rationale="test",
    )


# ---------------------------------------------------------------- catalogue


_PHASE3_FAULTS = (
    "power.brownout",
    "power.ramp",
    "power.cut",
    "sensor.dropout",
    "sensor.stuck",
    "time.ntp.cut",
    "time.clock.drift",
)


def test_all_phase3_faults_in_catalogue() -> None:
    for name in _PHASE3_FAULTS:
        assert name in CATALOGUE, f"{name} missing from catalogue"
        assert CATALOGUE[name].chaos_mesh_kind is None, f"{name} must not have a CRD kind"


def test_phase3_categories_assigned() -> None:
    assert CATALOGUE["power.brownout"].category == FaultCategory.POWER
    assert CATALOGUE["sensor.dropout"].category == FaultCategory.SENSOR
    # The two time.* hardware faults reuse the existing TIME enum but with
    # chaos_mesh_kind=None — same enum value, different agent path.
    assert CATALOGUE["time.ntp.cut"].category == FaultCategory.TIME


def test_high_risk_phase3_faults_require_approval() -> None:
    """Anything that modifies physics or rolls the clock far forward needs
    explicit sign-off; sensor-bus and NTP-firewall faults do not."""
    for name in ("power.brownout", "power.ramp", "power.cut", "time.clock.drift"):
        assert CATALOGUE[name].requires_approval is True, f"{name} should require approval"
    for name in ("sensor.dropout", "sensor.stuck", "time.ntp.cut"):
        assert CATALOGUE[name].requires_approval is False, f"{name} should NOT require approval"


# ---------------------------------------------------------------- registries


def test_all_phase3_renderers_registered() -> None:
    assert has_power_renderer("power.brownout")
    assert has_power_renderer("power.ramp")
    assert has_power_renderer("power.cut")
    assert has_sensor_renderer("sensor.dropout")
    assert has_sensor_renderer("sensor.stuck")
    assert has_time_renderer("time.ntp.cut")
    assert has_time_renderer("time.clock.drift")


# ---------------------------------------------------------------- power.*


def test_power_brownout_defaults() -> None:
    out = render_power_brownout(_spec("power.brownout"))
    assert out.name == "power.brownout"
    assert out.parameters == {"floor_mv": 2400, "rise_time_ms": 0}


def test_power_brownout_honors_overrides() -> None:
    out = render_power_brownout(_spec("power.brownout", {"floor_mv": 2000, "rise_time_ms": 50}))
    assert out.parameters == {"floor_mv": 2000, "rise_time_ms": 50}


def test_power_ramp_defaults() -> None:
    out = render_power_ramp(_spec("power.ramp"))
    assert out.parameters == {"start_mv": 5000, "floor_mv": 2500, "steps": 30}


def test_power_cut_has_no_parameters() -> None:
    out = render_power_cut(_spec("power.cut"))
    assert out.parameters == {}


def test_render_power_fault_dispatches() -> None:
    out = render_power_fault(_spec("power.brownout", {"floor_mv": 2200}))
    assert out.parameters["floor_mv"] == 2200


def test_render_power_fault_raises_for_unknown() -> None:
    with pytest.raises(KeyError, match="no power renderer"):
        render_power_fault(_spec("power.brownout").model_copy(update={"name": "power.bogus"}))


# ---------------------------------------------------------------- sensor.*


def test_sensor_dropout_defaults() -> None:
    out = render_sensor_dropout(_spec("sensor.dropout"))
    assert out.parameters == {"sensor_id": "primary", "bus": "i2c"}


def test_sensor_stuck_omits_replay_value_when_unset() -> None:
    """Renderer should omit replay_value rather than pin it to None so the
    bench knows to keep the previous reading rather than serve None."""
    out = render_sensor_stuck(_spec("sensor.stuck"))
    assert out.parameters == {"sensor_id": "primary"}
    assert "replay_value" not in out.parameters


def test_sensor_stuck_passes_through_replay_value() -> None:
    out = render_sensor_stuck(_spec("sensor.stuck", {"replay_value": 42.0}))
    assert out.parameters["replay_value"] == 42.0


def test_render_sensor_fault_dispatches() -> None:
    out = render_sensor_fault(_spec("sensor.dropout", {"sensor_id": "imu-1"}))
    assert out.parameters["sensor_id"] == "imu-1"


# ---------------------------------------------------------------- time.*


def test_time_ntp_cut_defaults() -> None:
    out = render_time_ntp_cut(_spec("time.ntp.cut"))
    assert out.parameters == {"ports": [123], "scope": "gateway"}


def test_time_ntp_cut_rejects_non_list_ports() -> None:
    with pytest.raises(ValueError, match="ports"):
        render_time_ntp_cut(_spec("time.ntp.cut", {"ports": 123}))


def test_time_clock_drift_defaults() -> None:
    """Default skew is one day forward — past every cert NotAfter."""
    out = render_time_clock_drift(_spec("time.clock.drift"))
    assert out.parameters == {"skew_seconds": 86_400, "ramp_rate": 0.0}


def test_time_clock_drift_passes_negative_skew() -> None:
    out = render_time_clock_drift(_spec("time.clock.drift", {"skew_seconds": -3600}))
    assert out.parameters["skew_seconds"] == -3600


def test_render_time_fault_raises_for_unknown() -> None:
    with pytest.raises(KeyError, match="no hardware time renderer"):
        render_time_fault(_spec("time.ntp.cut").model_copy(update={"name": "time.bogus"}))


# ---------------------------------------------------------------- simulator coupling


def test_simulator_brownout_bumps_boot_count_and_nvs_failures() -> None:
    sim = SimulatedHardwareIO()
    h = _run(sim.inject_fault(HardwareFault(name="power.brownout", parameters={}, duration_seconds=2)))
    boot = _run(sim.read_telemetry("boot_count_delta"))
    nvs = _run(sim.read_telemetry("nvs_write_failures"))
    assert boot.value == 1
    assert nvs.value == 1
    _run(sim.cleanup(h))
    # Counters are sticky after cleanup — they're cumulative, not gated by
    # active-fault status.
    boot = _run(sim.read_telemetry("boot_count_delta"))
    assert boot.value == 1


def test_simulator_power_cut_persists_event_buffer() -> None:
    sim = SimulatedHardwareIO()
    h = _run(sim.inject_fault(HardwareFault(name="power.cut", parameters={}, duration_seconds=2)))
    persisted = _run(sim.read_telemetry("event_buffer_persisted_count"))
    assert persisted.value == 1
    _run(sim.cleanup(h))


def test_simulator_sensor_dropout_degrades_mesh_consensus() -> None:
    sim = SimulatedHardwareIO()
    # Baseline.
    consensus = _run(sim.read_telemetry("mesh_consensus_degraded_count"))
    assert consensus.value == 0
    h = _run(sim.inject_fault(HardwareFault(name="sensor.dropout", parameters={}, duration_seconds=2)))
    consensus = _run(sim.read_telemetry("mesh_consensus_degraded_count"))
    assert consensus.value == 1
    _run(sim.cleanup(h))
    # Recovers on cleanup — the dropout is in-band, not a sticky event.
    consensus = _run(sim.read_telemetry("mesh_consensus_degraded_count"))
    assert consensus.value == 0


def test_simulator_sensor_stuck_drives_false_positive_rate_up() -> None:
    sim = SimulatedHardwareIO()
    baseline = _run(sim.read_telemetry("detector_false_positive_rate"))
    assert baseline.value == pytest.approx(0.001)
    h = _run(sim.inject_fault(HardwareFault(name="sensor.stuck", parameters={}, duration_seconds=2)))
    elevated = _run(sim.read_telemetry("detector_false_positive_rate"))
    assert elevated.value > baseline.value
    _run(sim.cleanup(h))


def test_simulator_ntp_cut_defers_cert_renewal() -> None:
    sim = SimulatedHardwareIO()
    assert _run(sim.read_telemetry("cert_renewal_deferred_count")).value == 0
    h = _run(sim.inject_fault(HardwareFault(name="time.ntp.cut", parameters={}, duration_seconds=2)))
    assert _run(sim.read_telemetry("cert_renewal_deferred_count")).value == 1
    _run(sim.cleanup(h))


def test_simulator_clock_drift_kills_cert_validity() -> None:
    sim = SimulatedHardwareIO()
    healthy = _run(sim.read_telemetry("cert_validity_remaining_h"))
    assert healthy.value > 24
    h = _run(sim.inject_fault(HardwareFault(name="time.clock.drift", parameters={}, duration_seconds=2)))
    drifted = _run(sim.read_telemetry("cert_validity_remaining_h"))
    assert drifted.value == 0
    failures = _run(sim.read_telemetry("cert_validation_failures"))
    assert failures.value == 1
    _run(sim.cleanup(h))


def test_simulator_wifi_jam_raises_gateway_rtt() -> None:
    sim = SimulatedHardwareIO()
    baseline = _run(sim.read_telemetry("gateway_uplink_rtt_ms"))
    assert baseline.value == 80
    h = _run(sim.inject_fault(HardwareFault(name="wifi.jam", parameters={}, duration_seconds=2)))
    elevated = _run(sim.read_telemetry("gateway_uplink_rtt_ms"))
    assert elevated.value > 1000
    _run(sim.cleanup(h))


# ---------------------------------------------------------------- agent end-to-end


def test_agent_executes_every_phase3_fault_end_to_end() -> None:
    """Every new fault must survive the agent's full execute() loop --
    catalogue -> renderer -> simulator inject -> cleanup -> success timeline."""
    for name in _PHASE3_FAULTS:
        sim = SimulatedHardwareIO()
        agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
        plan = ExperimentPlan(
            title=f"phase3-e2e-{name}",
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
        assert len(sim._active_faults) == 0
