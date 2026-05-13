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


def render_wifi_jam(fault: FaultSpec) -> HardwareFault:
    """Sweep a noise carrier across 2.4 GHz.

    Parameters:
        channel         int   single channel to jam; 0 = full sweep 1..14.
        power_dbm       int   transmit power; clamped at the attack
                              device's licensed maximum. Default 0 (~1 mW).
        sweep_period_ms int   ms between channel hops in sweep mode.
    """
    params = {
        "channel": int(fault.parameters.get("channel", 0)),
        "power_dbm": int(fault.parameters.get("power_dbm", 0)),
        "sweep_period_ms": int(fault.parameters.get("sweep_period_ms", 100)),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


def render_ble_advertising_flood(fault: FaultSpec) -> HardwareFault:
    """Emit a high-rate BLE advertising flood from spoofed MACs.

    Parameters:
        rate_per_second int   advertising packets per second. Default 10000.
        spoofed_macs    int   how many distinct MAC addresses to cycle
                              through. Default 256.
        adv_data_size   int   bytes of adv payload (per-packet). Default 28.
    """
    params = {
        "rate_per_second": int(fault.parameters.get("rate_per_second", 10000)),
        "spoofed_macs": int(fault.parameters.get("spoofed_macs", 256)),
        "adv_data_size": int(fault.parameters.get("adv_data_size", 28)),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


def render_lora_jam(fault: FaultSpec) -> HardwareFault:
    """Continuous carrier on the DUT's LoRa channel.

    Parameters:
        frequency_hz    int   center frequency. Default 915_000_000 (US).
        bandwidth_hz    int   carrier bandwidth. Default 125_000.
        spreading_factor int  SF7..SF12; default 7.
        power_dbm       int   transmit power; default 14.
    """
    params = {
        "frequency_hz": int(fault.parameters.get("frequency_hz", 915_000_000)),
        "bandwidth_hz": int(fault.parameters.get("bandwidth_hz", 125_000)),
        "spreading_factor": int(fault.parameters.get("spreading_factor", 7)),
        "power_dbm": int(fault.parameters.get("power_dbm", 14)),
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
    "wifi.jam": render_wifi_jam,
    "ble.advertising_flood": render_ble_advertising_flood,
    "lora.jam": render_lora_jam,
}


def has_rf_renderer(name: str) -> bool:
    return name in RF_RENDERERS


def render_rf_fault(fault: FaultSpec) -> HardwareFault:
    """Render any registered RF fault. Raises KeyError on unknown names."""
    if fault.name not in RF_RENDERERS:
        raise KeyError(f"no RF renderer for {fault.name!r}")
    return RF_RENDERERS[fault.name](fault)
