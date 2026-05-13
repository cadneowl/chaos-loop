"""
Power-rail fault renderers — produce a HardwareFault from a FaultSpec.

Three faults in Phase 3:

    power.brownout    drop the DUT's rail to `floor_mv` for `duration_seconds`
    power.ramp        slow-ramp from `start_mv` to `floor_mv` over the duration
    power.cut         hard cut for `duration_seconds`, then restore

Like the rf.* renderers, these produce a `HardwareFault` command spec
the HardwareIO implementation translates into bench commands. The
real-bench path drives a programmable PSU; the simulator path mutates
in-memory state that the corresponding metrics (boot_count,
nvs_write_failures, event_buffer_persisted_count) read back from.
"""

from __future__ import annotations

from collections.abc import Callable

from agents.chaos.hardware_io import HardwareFault
from shared.contracts import FaultSpec


def render_power_brownout(fault: FaultSpec) -> HardwareFault:
    """Drop the supply rail to a configured floor for the duration.

    Parameters:
        floor_mv          int   target voltage at the floor, in millivolts.
                                Default 2400 (just below ESP32 brownout).
        rise_time_ms      int   ms to ramp into the floor (0 = step).
    """
    params = {
        "floor_mv": int(fault.parameters.get("floor_mv", 2400)),
        "rise_time_ms": int(fault.parameters.get("rise_time_ms", 0)),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


def render_power_ramp(fault: FaultSpec) -> HardwareFault:
    """Slow ramp from start_mv → floor_mv over the duration.

    Parameters:
        start_mv          int   starting voltage, default 5000 (5V).
        floor_mv          int   ending voltage, default 2500.
        steps             int   discrete steps in the ramp, default 30.
    """
    params = {
        "start_mv": int(fault.parameters.get("start_mv", 5000)),
        "floor_mv": int(fault.parameters.get("floor_mv", 2500)),
        "steps": int(fault.parameters.get("steps", 30)),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


def render_power_cut(fault: FaultSpec) -> HardwareFault:
    """Hard supply cut for `duration_seconds`, then restore.

    No parameters — the cut is total. `duration_seconds` is the off
    interval; the bench restores the rail when the duration elapses.
    """
    return HardwareFault(
        name=fault.name,
        parameters={},
        duration_seconds=fault.duration_seconds,
    )


POWER_RENDERERS: dict[str, Callable[[FaultSpec], HardwareFault]] = {
    "power.brownout": render_power_brownout,
    "power.ramp": render_power_ramp,
    "power.cut": render_power_cut,
}


def has_power_renderer(name: str) -> bool:
    return name in POWER_RENDERERS


def render_power_fault(fault: FaultSpec) -> HardwareFault:
    """Render any registered power fault. Raises KeyError on unknown names."""
    if fault.name not in POWER_RENDERERS:
        raise KeyError(f"no power renderer for {fault.name!r}")
    return POWER_RENDERERS[fault.name](fault)
