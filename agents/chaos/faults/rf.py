"""
RF fault renderers — produce a HardwareFault from a FaultSpec.

Unlike the Kubernetes renderers in `network.py`/`pod.py`/`cert.py` etc.
(which return a Chaos Mesh CRD body), these return a `HardwareFault`
command spec that the HardwareIO implementation translates into wire
bytes for the attack device.

Each renderer is registered in `RF_RENDERERS` keyed by the fault name
in the catalogue. `HardwareChaosAgent` consults this registry directly,
isolated from the kubernetes-renderer registry in `registry.py`.

For Phase 1 there's a single renderer: `wifi.deauth`. Phase 2 will
add `wifi.jam`, `ble.advertising_flood`, `lora.jam`.
"""

from __future__ import annotations

from collections.abc import Callable

from agents.chaos.hardware_io import HardwareFault
from shared.contracts import FaultSpec


def render_wifi_deauth(fault: FaultSpec) -> HardwareFault:
    """Render a `wifi.deauth` fault into the attack-device command.

    Parameters honored from the FaultSpec:
        target_bssid    str   the AP MAC to deauth from. "auto" picks
                              the DUT's current BSSID.
        channel         int   802.11 channel; 0 = sweep.
        intensity       str   "low" | "medium" | "high"

    All optional; defaults give a moderate deauth flood at the DUT's
    current AP.
    """
    params = {
        "target_bssid": str(fault.parameters.get("target_bssid", "auto")),
        "channel": int(fault.parameters.get("channel", 0)),
        "intensity": str(fault.parameters.get("intensity", "medium")),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


# Local registry — keep separate from the kubernetes registry in
# `agents/chaos/faults/registry.py` so neither path leaks into the other.
RF_RENDERERS: dict[str, Callable[[FaultSpec], HardwareFault]] = {
    "wifi.deauth": render_wifi_deauth,
}


def has_rf_renderer(name: str) -> bool:
    return name in RF_RENDERERS


def render_rf_fault(fault: FaultSpec) -> HardwareFault:
    """Render any registered RF fault. Raises KeyError on unknown names."""
    if fault.name not in RF_RENDERERS:
        raise KeyError(f"no RF renderer for {fault.name!r}")
    return RF_RENDERERS[fault.name](fault)
