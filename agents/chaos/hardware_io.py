"""
HardwareIO — the embedded analog of ClusterIO.

Where ClusterIO talks to the Kubernetes API to apply CRDs and read pod
status, HardwareIO talks to a device-under-test (DUT) and an attack
device for fault injection.

Same architectural pattern: a thin Protocol with a fake implementation
for tests (`SimulatedHardwareIO`) and a real implementation for actual
benches (`HilHardwareIO`, Phase 2). The chaos agent can't tell them
apart — only `execute()` shape matters.

What lives behind this Protocol depends on the fault category:
    - rf.*       attack device emits RF (deauth frames, jam carrier, BLE flood)
    - power.*    bench power supply ramps / cuts the DUT's rail (Phase 3)
    - sensor.*   inline mux disconnects / spoofs a sensor bus (Phase 3)
    - time.*     gateway-side firewall blocks NTP or injects a skewed server (Phase 3)

For Phase 1 we ship a single fault (`rf.wifi.deauth`) and a single
implementation (`SimulatedHardwareIO`) — enough to validate the wiring
end to end without a real ESP32 plugged in.

See docs/NEOOWL_ADAPTATION.md for the full plan.
"""

from __future__ import annotations

import asyncio
import time as time_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class DeviceInfo:
    """Identity + mode of the device the bench is talking to.

    `mode` discriminates BENCH (chaos is permitted) from PRODUCTION
    (firmware rejects all chaos commands at the device level; the
    orchestrator also gates this on the python side).
    """

    serial: str
    firmware_version: str
    hardware_revision: str
    mode: str  # "BENCH" | "PRODUCTION" | "UNKNOWN"


@dataclass(frozen=True)
class TelemetrySample:
    """A single reading from the DUT's telemetry endpoint.

    Maps onto the tester's `InstantSample` shape so the existing probe
    plumbing accepts hardware readings without modification.
    """

    metric: str
    value: float
    timestamp: float = field(default_factory=time_module.time)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HardwareFault:
    """A fault rendered to the command shape the attack device understands.

    The chaos agent's hardware renderer produces one of these per
    `FaultSpec`; the IO implementation translates it into bench-specific
    wire bytes.
    """

    name: str
    parameters: dict[str, Any]
    duration_seconds: int


@dataclass(frozen=True)
class InjectionHandle:
    """Opaque ID returned by `inject_fault`; passed back to `cleanup`.

    Implementations stuff whatever they need into it (a serial-port file
    handle, a remote run-id, etc.). Treat as opaque from outside.
    """

    id: str


# ---------------------------------------------------------------------- Protocol


class HardwareIO(Protocol):
    """Minimal hardware-bench interface used by HardwareChaosAgent + tester probes."""

    async def device_info(self) -> DeviceInfo: ...
    """Identity + mode of the DUT. Used by safety gates pre-flight."""

    async def read_telemetry(self, metric: str) -> TelemetrySample: ...
    """One-shot read of a metric. Used by the tester's probe loop."""

    async def inject_fault(self, fault: HardwareFault) -> InjectionHandle: ...
    """Start a fault on the attack device. Returns a handle for cleanup.

    The call returns once the fault is *active*, not when it's done.
    The chaos agent sleeps for `fault.duration_seconds` and then calls
    `cleanup(handle)`."""

    async def cleanup(self, handle: InjectionHandle) -> None: ...
    """Stop a fault and tear down anything the inject_fault put in place.

    Idempotent: a repeated cleanup must not raise (in case the fault
    timed out on its own before we got back to it)."""

    async def reset(self) -> None: ...
    """Hard-reset the DUT. Used before BASELINE and after ABORTED."""


# ---------------------------------------------------------------------- Simulator


@dataclass
class SimulatedHardwareIO:
    """In-memory simulator. Models a NeoOwl-style detector.

    Steady-state detector latency tracks `_baseline_latency_ms` (default
    200ms with a little noise). When a `wifi.deauth` fault is active, the
    latency degrades to `_degraded_latency_ms` (default 1400ms). On
    cleanup, latency returns to baseline within one telemetry read.

    Used to exercise the full orchestrator loop without a real ESP32.
    Same Protocol shape as the eventual `HilHardwareIO` (Phase 2), so the
    chaos agent and tester need no changes when we swap in real hardware.
    """

    device: DeviceInfo = field(
        default_factory=lambda: DeviceInfo(
            serial="sim-DUT-001",
            firmware_version="0.1.0-sim",
            hardware_revision="rev-A",
            mode="BENCH",
        )
    )

    _baseline_latency_ms: float = 200.0
    _degraded_latency_ms: float = 1400.0
    _active_faults: dict[str, HardwareFault] = field(default_factory=dict)
    _next_handle: int = 0
    _reset_count: int = 0

    # Hooks for tests that want to override behavior.
    inject_hook: Callable[[HardwareFault], Awaitable[None]] | None = None

    async def device_info(self) -> DeviceInfo:
        return self.device

    async def read_telemetry(self, metric: str) -> TelemetrySample:
        # The probe set asks for `detector_latency_p95_ms` today; future
        # probes will read different metric names from this same surface.
        if metric == "detector_latency_p95_ms":
            value = self._current_latency_ms()
        elif metric == "boot_count":
            value = float(self._reset_count)
        else:
            # Unknown metric: return 0 rather than raise, mirroring how
            # Prometheus returns an empty vector for unknown queries.
            value = 0.0
        return TelemetrySample(metric=metric, value=value, labels={"device": self.device.serial})

    async def inject_fault(self, fault: HardwareFault) -> InjectionHandle:
        if self.inject_hook is not None:
            await self.inject_hook(fault)
        self._next_handle += 1
        handle = InjectionHandle(id=f"sim-inject-{self._next_handle:04d}")
        self._active_faults[handle.id] = fault
        # In a real bench inject_fault would block until the attack
        # device confirms it's transmitting; for the simulator we yield
        # once so the event-loop ordering matches real-world timing.
        await asyncio.sleep(0)
        return handle

    async def cleanup(self, handle: InjectionHandle) -> None:
        # Idempotent — already-removed is a no-op, matching the contract.
        self._active_faults.pop(handle.id, None)
        await asyncio.sleep(0)

    async def reset(self) -> None:
        self._active_faults.clear()
        self._reset_count += 1
        await asyncio.sleep(0)

    # ------- helpers (sim-only) -------

    def _current_latency_ms(self) -> float:
        return (
            self._degraded_latency_ms
            if self._has_active_fault("wifi.deauth")
            else self._baseline_latency_ms
        )

    def _has_active_fault(self, name: str) -> bool:
        return any(f.name == name for f in self._active_faults.values())
