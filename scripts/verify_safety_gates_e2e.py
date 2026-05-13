"""End-to-end safety-gate smoke test.

For each of the 5 hardware safety gates, build a SimulatedHardwareIO in
the degraded state that should trip the gate, run the chaos agent against
a representative plan, and assert the timeline records the expected
abort reason. Used to verify the gates are wired into the agent's
pre-flight (not just unit-tested in isolation).

Run with:
    .venv/Scripts/python.exe scripts/verify_safety_gates_e2e.py
"""

from __future__ import annotations

import asyncio
import sys

from agents.chaos.hardware_agent import HardwareChaosAgent
from agents.chaos.hardware_io import DeviceInfo, SimulatedHardwareIO
from agents.chaos.hardware_safety import HardwareSafetyConfig
from shared.contracts import (
    ExperimentPlan,
    FaultSpec,
    SafetyConstraints,
)


def _wifi_fault(channel: int = 6) -> FaultSpec:
    return FaultSpec(
        category="rf",  # type: ignore[arg-type]
        name="wifi.deauth",
        target_selector={"device": "dut-1"},
        parameters={"channel": channel, "intensity": "low"},
        duration_seconds=5,
        requires_approval=False,
        rationale="safety-gate verification",
    )


def _plan(fault: FaultSpec | None = None) -> ExperimentPlan:
    return ExperimentPlan(
        title="safety-gate verification",
        target_app="neoowl",
        faults=[fault or _wifi_fault()],
        safety=SafetyConstraints(
            cluster_context="bench-hardware",
            namespace="bench",
            require_namespace_annotation=False,
        ),
    )


async def _no_sleep(_secs: float) -> None:
    return None


async def _check(scenario: str, sim: SimulatedHardwareIO, config: HardwareSafetyConfig,
                  plan: ExperimentPlan, expect_substr: str) -> tuple[bool, str]:
    agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep, safety_config=config)
    timeline = await agent.execute(plan)
    if timeline.success:
        return False, f"{scenario}: expected failure but timeline succeeded"
    err = (timeline.error or "").lower()
    if expect_substr.lower() not in err:
        return False, f"{scenario}: error did not mention {expect_substr!r}; got {timeline.error!r}"
    return True, f"{scenario}: aborted with {timeline.error!r}"


async def main() -> int:
    results: list[tuple[bool, str]] = []

    # 1. bench-mode gate -- DUT reports PRODUCTION
    sim = SimulatedHardwareIO(
        device=DeviceInfo(
            serial="prod-DUT-001",
            firmware_version="2.0.0",
            hardware_revision="rev-C",
            mode="PRODUCTION",
        )
    )
    results.append(await _check(
        "bench-mode", sim, HardwareSafetyConfig(), _plan(),
        "bench",
    ))

    # 2. geofence gate -- bench has drifted
    sim = SimulatedHardwareIO(geofence_tag="kitchen-table")
    results.append(await _check(
        "geofence", sim,
        HardwareSafetyConfig(expected_geofence_tag="lab-bench-12"),
        _plan(),
        "geofence",
    ))

    # 3. thermal gate -- die temp too high
    sim = SimulatedHardwareIO()
    sim.metric_overrides["die_temperature_c"] = 85.0
    results.append(await _check(
        "thermal", sim, HardwareSafetyConfig(), _plan(),
        "die",
    ))

    # 4. battery gate -- SoC too low
    sim = SimulatedHardwareIO()
    sim.metric_overrides["battery_soc"] = 0.10
    results.append(await _check(
        "battery", sim, HardwareSafetyConfig(), _plan(),
        "battery",
    ))

    # 5. emission compliance -- channel outside licensed band
    sim = SimulatedHardwareIO()
    results.append(await _check(
        "emission-band", sim,
        HardwareSafetyConfig(licensed_bands=((1, 14),)),
        _plan(_wifi_fault(channel=36)),
        "channel 36",
    ))

    # Summary
    for ok, msg in results:
        marker = "OK  " if ok else "FAIL"
        print(f"{marker}  {msg}")
    failed = [m for ok, m in results if not ok]
    if failed:
        print(f"\n{len(failed)} of {len(results)} gates FAILED to abort correctly")
        return 1
    print(f"\nAll {len(results)} safety gates aborted as expected")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
