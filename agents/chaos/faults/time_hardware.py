"""
Time-source hardware fault renderers — produce a HardwareFault from a FaultSpec.

Two faults in Phase 3:

    time.ntp.cut       firewall NTP traffic at the gateway
    time.clock.drift   inject a fake NTP server with skew

Distinct from `time.skew` (Chaos Mesh TimeChaos) — these run on the
hardware-bench path. The module is named `time_hardware` to avoid
shadowing stdlib `time` when imported alongside other faults modules.
"""

from __future__ import annotations

from collections.abc import Callable

from agents.chaos.hardware_io import HardwareFault
from shared.contracts import FaultSpec


def render_time_ntp_cut(fault: FaultSpec) -> HardwareFault:
    """Block outbound NTP at the gateway for the duration.

    Parameters:
        ports        list[int]   UDP ports to firewall; default [123].
        scope        str         "gateway" (default) | "dut".
    """
    raw_ports = fault.parameters.get("ports", [123])
    if not isinstance(raw_ports, list):
        raise ValueError("time.ntp.cut: `ports` must be a list")
    params = {
        "ports": [int(p) for p in raw_ports],
        "scope": str(fault.parameters.get("scope", "gateway")),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


def render_time_clock_drift(fault: FaultSpec) -> HardwareFault:
    """Inject a fake NTP server with a configurable skew.

    Parameters:
        skew_seconds   int    seconds of skew to inject. Positive =
                              future, negative = past. Default 86400
                              (one day forward — past every cert NotAfter).
        ramp_rate      float  optional ramp speed in seconds-of-drift
                              per real second. Default 0 = step.
    """
    params = {
        "skew_seconds": int(fault.parameters.get("skew_seconds", 86_400)),
        "ramp_rate": float(fault.parameters.get("ramp_rate", 0.0)),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


TIME_RENDERERS: dict[str, Callable[[FaultSpec], HardwareFault]] = {
    "time.ntp.cut": render_time_ntp_cut,
    "time.clock.drift": render_time_clock_drift,
}


def has_time_renderer(name: str) -> bool:
    return name in TIME_RENDERERS


def render_time_fault(fault: FaultSpec) -> HardwareFault:
    """Render any registered hardware time fault. Raises KeyError on unknown names."""
    if fault.name not in TIME_RENDERERS:
        raise KeyError(f"no hardware time renderer for {fault.name!r}")
    return TIME_RENDERERS[fault.name](fault)
