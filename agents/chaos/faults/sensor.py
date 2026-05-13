"""
Sensor-bus fault renderers — produce a HardwareFault from a FaultSpec.

Two faults in Phase 3:

    sensor.dropout    physically disconnect the sensor's I²C/SPI bus
    sensor.stuck      intercept the bus, replay the last observed reading

The bench-side implementation uses an inline mux on the sensor bus; the
simulator path just mutates the readings the corresponding metrics
return (mesh_consensus_degraded_count, anomaly_detector_fired).
"""

from __future__ import annotations

from collections.abc import Callable

from agents.chaos.hardware_io import HardwareFault
from shared.contracts import FaultSpec


def render_sensor_dropout(fault: FaultSpec) -> HardwareFault:
    """Disconnect the sensor's bus for the duration.

    Parameters:
        sensor_id     str   identifier of the sensor to disconnect.
                            Default "primary" (sensor 0 in the mux).
        bus           str   "i2c" | "spi"; default "i2c".
    """
    params = {
        "sensor_id": str(fault.parameters.get("sensor_id", "primary")),
        "bus": str(fault.parameters.get("bus", "i2c")),
    }
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


def render_sensor_stuck(fault: FaultSpec) -> HardwareFault:
    """Replay the last observed reading for the duration.

    Parameters:
        sensor_id          str    identifier; default "primary".
        replay_value       float  override the replayed value rather than
                                  using the last observed one. None = use
                                  whatever was last on the bus.
    """
    params: dict[str, object] = {
        "sensor_id": str(fault.parameters.get("sensor_id", "primary")),
    }
    if "replay_value" in fault.parameters:
        params["replay_value"] = float(fault.parameters["replay_value"])
    return HardwareFault(
        name=fault.name,
        parameters=params,
        duration_seconds=fault.duration_seconds,
    )


SENSOR_RENDERERS: dict[str, Callable[[FaultSpec], HardwareFault]] = {
    "sensor.dropout": render_sensor_dropout,
    "sensor.stuck": render_sensor_stuck,
}


def has_sensor_renderer(name: str) -> bool:
    return name in SENSOR_RENDERERS


def render_sensor_fault(fault: FaultSpec) -> HardwareFault:
    """Render any registered sensor fault. Raises KeyError on unknown names."""
    if fault.name not in SENSOR_RENDERERS:
        raise KeyError(f"no sensor renderer for {fault.name!r}")
    return SENSOR_RENDERERS[fault.name](fault)
