"""Unit tests for HardwareIO Protocol + SimulatedHardwareIO."""

from __future__ import annotations

import asyncio

import pytest

from agents.chaos.hardware_io import (
    HardwareFault,
    SimulatedHardwareIO,
)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_simulator_reports_baseline_latency_when_idle() -> None:
    sim = SimulatedHardwareIO()
    sample = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample.metric == "detector_latency_p95_ms"
    assert sample.value == sim._baseline_latency_ms
    assert sample.labels["device"] == sim.device.serial


def test_simulator_reports_degraded_latency_during_wifi_deauth() -> None:
    sim = SimulatedHardwareIO()
    fault = HardwareFault(
        name="wifi.deauth",
        parameters={"target_bssid": "auto"},
        duration_seconds=10,
    )
    handle = _run(sim.inject_fault(fault))
    assert handle.id.startswith("sim-inject-")

    # While the fault is active, telemetry reports the degraded value.
    sample = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample.value == sim._degraded_latency_ms

    # Cleanup restores baseline within one read.
    _run(sim.cleanup(handle))
    sample_after = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample_after.value == sim._baseline_latency_ms


def test_simulator_cleanup_is_idempotent() -> None:
    """Calling cleanup twice (e.g., on the abort path) must not raise."""
    sim = SimulatedHardwareIO()
    fault = HardwareFault(name="wifi.deauth", parameters={}, duration_seconds=1)
    handle = _run(sim.inject_fault(fault))
    _run(sim.cleanup(handle))
    _run(sim.cleanup(handle))  # second call — would be the issue


def test_simulator_supports_multiple_concurrent_handles() -> None:
    """Two faults at once: each gets a unique handle, both must be cleaned
    up to restore baseline."""
    sim = SimulatedHardwareIO()
    fault = HardwareFault(name="wifi.deauth", parameters={}, duration_seconds=10)
    h1 = _run(sim.inject_fault(fault))
    h2 = _run(sim.inject_fault(fault))
    assert h1.id != h2.id
    sample = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample.value == sim._degraded_latency_ms
    # Remove only one — the other is still active.
    _run(sim.cleanup(h1))
    sample_still_degraded = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample_still_degraded.value == sim._degraded_latency_ms
    _run(sim.cleanup(h2))
    sample_clean = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample_clean.value == sim._baseline_latency_ms


def test_simulator_reset_clears_active_faults_and_increments_boot_count() -> None:
    sim = SimulatedHardwareIO()
    fault = HardwareFault(name="wifi.deauth", parameters={}, duration_seconds=10)
    _run(sim.inject_fault(fault))
    _run(sim.reset())
    sample = _run(sim.read_telemetry("detector_latency_p95_ms"))
    assert sample.value == sim._baseline_latency_ms
    boot = _run(sim.read_telemetry("boot_count"))
    assert boot.value == 1.0


def test_simulator_unknown_metric_returns_zero_not_error() -> None:
    """Mirrors Prometheus behavior — unknown query returns empty/zero rather
    than raising, so probe sets that ask about not-yet-implemented metrics
    fail open at the threshold check level rather than crashing the loop."""
    sim = SimulatedHardwareIO()
    sample = _run(sim.read_telemetry("nonexistent_metric"))
    assert sample.value == 0.0


def test_simulator_device_info_reports_bench_mode() -> None:
    """Bench-mode discriminator is what the hardware safety gate (Phase 2)
    will read to refuse running chaos against a production-mode DUT."""
    sim = SimulatedHardwareIO()
    info = _run(sim.device_info())
    assert info.mode == "BENCH"
    assert info.serial == "sim-DUT-001"


@pytest.mark.asyncio
async def test_simulator_inject_hook_fires_for_test_injection() -> None:
    """Lets a test inject a transient failure into the inject path (e.g.,
    simulate the attack device being unavailable for the first call)."""
    called: list[str] = []

    async def hook(fault: HardwareFault) -> None:
        called.append(fault.name)

    sim = SimulatedHardwareIO(inject_hook=hook)
    fault = HardwareFault(name="wifi.deauth", parameters={}, duration_seconds=1)
    await sim.inject_fault(fault)
    assert called == ["wifi.deauth"]
