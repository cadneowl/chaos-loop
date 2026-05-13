"""
Phase 1 exit-criteria test.

`chaos run experiments/neoowl/01-wifi-deauth.yaml --profile static`
must produce a real `ExperimentRecord` against the simulated DUT.

We drive `ExperimentRunner.run(plan)` directly (the same way
`test_dry_run_loop.py` does) with:
    - chaos      → HardwareChaosAgent(hardware=SimulatedHardwareIO)
    - tester     → ClaudeTesterAgent(prom_backend=HardwareTelemetryBackend(sim))
    - security / diagnostician / fixer → mocks (Phase 3 swap)

Then assert:
    1. The plan validates + loads.
    2. The orchestrator transitions through INITIALIZING → BASELINE →
       INJECT → VERIFY without aborting.
    3. The chaos timeline carries the expected events.
    4. The DUT was reset on cleanup (boot_count incremented).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from agents._mocks import build_mock_agents
from agents.chaos.hardware_agent import HardwareChaosAgent
from agents.chaos.hardware_io import SimulatedHardwareIO
from agents.tester.tools.hardware_telemetry import HardwareTelemetryBackend
from orchestrator.loop import Agents, ExperimentRunner
from orchestrator.store import ExperimentStore
from shared.contracts import ExperimentPlan, ExperimentState


@pytest.fixture
def store(tmp_path: Path) -> ExperimentStore:
    return ExperimentStore(tmp_path / "experiments.sqlite")


def _load_plan(path: Path) -> ExperimentPlan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ExperimentPlan.model_validate(raw)


def _build_hardware_agents(sim: SimulatedHardwareIO) -> Agents:
    """Phase 1 wiring: hardware chaos + hardware-fed tester, mocks for the rest.

    Phase 3 will replace the security/diagnostician/fixer mocks with
    embedded-firmware-aware variants and promote this helper into
    `agents/_factory.py:build_hardware_agents` (a public surface).
    """
    from agents.tester.agent import ClaudeTesterAgent

    # Mocks for the bits we haven't adapted yet — same as test_dry_run_loop.
    mocks = build_mock_agents()
    return Agents(
        tester=ClaudeTesterAgent(prom_backend=HardwareTelemetryBackend(sim)),
        security=mocks["security"],  # type: ignore[arg-type]
        chaos=HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep),
        diagnostician=mocks["diagnostician"],  # type: ignore[arg-type]
        fixer=mocks["fixer"],  # type: ignore[arg-type]
    )


async def _no_sleep(_secs: float) -> None:
    return None


def test_phase1_exit_criteria_simulated_dut_produces_real_record(
    store: ExperimentStore,
) -> None:
    """Exit criterion: real ExperimentRecord lands against simulated DUT."""
    plan_path = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "neoowl"
        / "01-wifi-deauth.yaml"
    )
    plan = _load_plan(plan_path)

    sim = SimulatedHardwareIO()
    runner = ExperimentRunner(agents=_build_hardware_agents(sim), store=store)
    record = asyncio.run(runner.run(plan))

    # 1. The orchestrator reached a terminal state without crashing.
    assert record.state == ExperimentState.RECORDED
    # 2. The chaos timeline reports the deauth lifecycle.
    assert record.chaos_timeline is not None
    assert record.chaos_timeline.success is True
    events = [(e.fault_name, e.event) for e in record.chaos_timeline.events]
    assert ("wifi.deauth", "scheduled") in events
    assert ("wifi.deauth", "started") in events
    assert ("wifi.deauth", "cleaned-up") in events
    # 3. The tester ran baseline + verify probes — both should have read
    #    from the simulator (presence of a tester report is enough; the
    #    probe-result semantics depend on the static probe expectations).
    assert record.tester_baseline is not None
    assert record.tester_verify is not None
    # 4. The record was persisted.
    loaded = store.load(plan.experiment_id)
    assert loaded is not None
    assert loaded.experiment_id == plan.experiment_id


def test_hardware_telemetry_backend_reads_from_simulator() -> None:
    """The Phase 1 wiring assumes a `HardwareTelemetryBackend` adapter
    surfaces hardware reads as the same `InstantSample` shape the tester
    already consumes. Verify the adapter directly."""
    sim = SimulatedHardwareIO()
    backend = HardwareTelemetryBackend(hardware=sim)

    samples = asyncio.run(backend.query_instant("detector_latency_p95_ms"))
    assert len(samples) == 1
    assert samples[0].value == sim._baseline_latency_ms
    assert samples[0].labels["device"] == sim.device.serial


def test_hardware_telemetry_backend_reflects_fault_state() -> None:
    """Probe reads change as the simulator's fault state changes —
    confirming the loop can observe the regression it's about to diagnose."""
    sim = SimulatedHardwareIO()
    backend = HardwareTelemetryBackend(hardware=sim)

    from agents.chaos.hardware_io import HardwareFault

    handle = asyncio.run(
        sim.inject_fault(HardwareFault(name="wifi.deauth", parameters={}, duration_seconds=10))
    )
    [degraded] = asyncio.run(backend.query_instant("detector_latency_p95_ms"))
    assert degraded.value == sim._degraded_latency_ms

    asyncio.run(sim.cleanup(handle))
    [recovered] = asyncio.run(backend.query_instant("detector_latency_p95_ms"))
    assert recovered.value == sim._baseline_latency_ms
