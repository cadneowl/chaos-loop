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

from agents.chaos.hil_transport import (
    HttpTransport,
    SerialTransport,
    decode,
    encode,
)


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
    # Geofence tag the simulator advertises; tests can override to force
    # the geofence gate to fail. Default matches HardwareSafetyConfig's
    # default skip behavior (config tag=None means "don't check").
    geofence_tag: str = "lab-bench-default"
    # Per-test metric overrides. e.g. `metric_overrides["battery_soc"] = 0.1`
    # to force the battery gate to refuse.
    metric_overrides: dict[str, float] = field(default_factory=dict)

    # Hooks for tests that want to override behavior.
    inject_hook: Callable[[HardwareFault], Awaitable[None]] | None = None

    async def device_info(self) -> DeviceInfo:
        return self.device

    async def read_telemetry(self, metric: str) -> TelemetrySample:
        # The probe set asks for `detector_latency_p95_ms`; the hardware
        # safety gates ask for `die_temperature_c` / `battery_soc` /
        # `geofence_tag`. Future probes will read more metric names from
        # this same surface. Sane defaults represent a healthy benched
        # DUT — tests can override by reaching into `sim.metric_overrides`.
        labels = {"device": self.device.serial}
        if metric in self.metric_overrides:
            return TelemetrySample(metric=metric, value=self.metric_overrides[metric], labels=labels)
        if metric == "detector_latency_p95_ms":
            value = self._current_latency_ms()
        elif metric == "boot_count":
            value = float(self._reset_count)
        elif metric == "die_temperature_c":
            value = 35.0  # healthy, well under the 70°C threshold
        elif metric == "battery_soc":
            value = 0.90  # healthy, well over the 30% threshold
        elif metric == "geofence_tag":
            # String-valued metric: encode the tag via labels rather than value.
            labels = {**labels, "tag": self.geofence_tag}
            value = 1.0
        else:
            # Unknown metric: return 0 rather than raise, mirroring how
            # Prometheus returns an empty vector for unknown queries.
            value = 0.0
        return TelemetrySample(metric=metric, value=value, labels=labels)

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
        # Any of the WiFi-degradation faults degrade detector latency. LoRa
        # and BLE faults exercise other subsystems modeled by other metrics
        # (Phase 3 will surface ble_scan_queue_depth, lora_packet_loss_pct).
        if self._has_active_fault("wifi.deauth") or self._has_active_fault("wifi.jam"):
            return self._degraded_latency_ms
        return self._baseline_latency_ms

    def _has_active_fault(self, name: str) -> bool:
        return any(f.name == name for f in self._active_faults.values())


# ---------------------------------------------------------------------- HIL bench


@dataclass
class HilHardwareIO:
    """Hardware-in-the-loop bench backend.

    Talks to two transports:
        attacker   — JSON-line serial to the attack-ESP32 (commands +
                     acks for inject / cleanup / info / reset)
        dut        — HTTP to the device-under-test's telemetry endpoint
                     (existing NeoOwl firmware already exposes one)

    The Protocol surface (device_info / read_telemetry / inject_fault /
    cleanup / reset) is the same as `SimulatedHardwareIO`'s, so callers
    (the chaos agent, the tester telemetry adapter, the safety gates)
    can't tell them apart. Tests swap the transports for the fakes in
    `hil_transport.py`.

    Wire protocol with the attacker:
        send: {"cmd":"info"}
        recv: {"ok":true,"firmware":"...","serial":"...","mode":"BENCH"}
        send: {"cmd":"inject","fault":"wifi.deauth",
               "params":{...},"duration_seconds":30}
        recv: {"ok":true,"handle":"h-0042"}
        send: {"cmd":"cleanup","handle":"h-0042"}
        recv: {"ok":true}
        send: {"cmd":"reset"}
        recv: {"ok":true}

    DUT telemetry shape (HTTP):
        GET <telemetry_base>/<metric>  →  {"metric":"…","value":1234.5,"labels":{"device":"…"}}

    Errors from the attacker (any `{"ok":false,"error":"…"}`) raise
    `HilTransportError`. Connection issues raise the transport's native
    error type, which the chaos agent wraps into a ChaosTimeline failure.
    """

    attacker: SerialTransport
    dut: HttpTransport
    telemetry_base_url: str = "http://localhost:8080/telemetry"

    async def device_info(self) -> DeviceInfo:
        await self.attacker.send_line(encode({"cmd": "info"}))
        reply = decode(await self.attacker.recv_line())
        if not reply.get("ok"):
            raise HilTransportError(reply.get("error", "info refused"))
        return DeviceInfo(
            serial=str(reply.get("serial", "unknown")),
            firmware_version=str(reply.get("firmware", "unknown")),
            hardware_revision=str(reply.get("hardware", "unknown")),
            mode=str(reply.get("mode", "UNKNOWN")),
        )

    async def read_telemetry(self, metric: str) -> TelemetrySample:
        url = f"{self.telemetry_base_url}/{metric}"
        payload = await self.dut.get_json(url)
        return TelemetrySample(
            metric=str(payload.get("metric", metric)),
            value=float(payload.get("value", 0.0)),
            labels=dict(payload.get("labels", {})),
        )

    async def inject_fault(self, fault: HardwareFault) -> InjectionHandle:
        await self.attacker.send_line(
            encode(
                {
                    "cmd": "inject",
                    "fault": fault.name,
                    "params": dict(fault.parameters),
                    "duration_seconds": fault.duration_seconds,
                }
            )
        )
        reply = decode(await self.attacker.recv_line(timeout_s=10.0))
        if not reply.get("ok"):
            raise HilTransportError(reply.get("error", "inject refused"))
        return InjectionHandle(id=str(reply["handle"]))

    async def cleanup(self, handle: InjectionHandle) -> None:
        await self.attacker.send_line(encode({"cmd": "cleanup", "handle": handle.id}))
        reply = decode(await self.attacker.recv_line())
        # Cleanup is idempotent at the agent layer; if the attacker says
        # "already gone" we treat that as success.
        if not reply.get("ok"):
            err = str(reply.get("error", ""))
            if "already" not in err.lower():
                raise HilTransportError(err or "cleanup refused")

    async def reset(self) -> None:
        await self.attacker.send_line(encode({"cmd": "reset"}))
        reply = decode(await self.attacker.recv_line())
        if not reply.get("ok"):
            raise HilTransportError(reply.get("error", "reset refused"))


class HilTransportError(RuntimeError):
    """Raised when the attack device or DUT replies with an error or garbage."""
