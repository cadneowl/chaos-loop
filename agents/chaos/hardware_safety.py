"""
Hardware safety gates — pre-flight checks specific to chaos against
physical electronics.

Cloud chaos can tolerate "delete the pod and see what happens." Hardware
cannot: a brownout cascade on an under-charged battery bricks the device;
a `wifi.deauth` test outside an anechoic chamber may violate licensed-band
limits; thermal stress without headroom destroys silicon.

Five gates, all deterministic Python (no LLM), all audit-friendly. They
extend the existing `orchestrator/safety.py` pattern — same `GateFailure`
shape, same return semantics — but read DUT state via a `HardwareIO`
instead of plan-only fields.

The gates are evaluated by `HardwareChaosAgent` pre-flight; a failure
short-circuits to a failed `ChaosTimeline` with a descriptive error,
which the orchestrator's `_abort` then handles the same way it handles
any other gate.

See docs/NEOOWL_ADAPTATION.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.chaos.hardware_io import HardwareIO
from orchestrator.safety import GateFailure
from shared.contracts import AbortReason, ExperimentPlan

# Thresholds. Conservative defaults; can be relaxed per-plan in a future
# revision via `plan.safety.hardware.*` fields once the schema admits them.
_THERMAL_MAX_C: float = 70.0
_BATTERY_MIN_SOC: float = 0.30  # 30%


@dataclass(frozen=True)
class HardwareSafetyConfig:
    """Bench-environment expectations the operator declares ahead of a run.

    `expected_serial` ties a plan to a specific DUT — the bench-mode
    check refuses to run if the plugged-in device isn't the one the
    plan was authored for. Set to None to accept any DUT.

    `expected_geofence_tag` is the GPS/WiFi fingerprint label the bench
    advertises in its telemetry. The geofence check refuses to run if
    the DUT reports a different fingerprint (e.g., the device walked
    out of the lab). Set to None to skip the check.

    `licensed_bands` lists 802.11 channel ranges the operator is
    licensed to transmit on, e.g. `[(1, 14)]` for ISM 2.4 GHz globally
    or `[(36, 165)]` for U-NII bands. An `rf.*` fault declaring a
    channel outside the license fails the emission check.

    Default values reflect a US-default ISM-only bench.
    """

    expected_serial: str | None = None
    expected_geofence_tag: str | None = None
    licensed_bands: tuple[tuple[int, int], ...] = ((1, 14),)


# ---------------------------------------------------------------- gates


async def check_bench_mode(
    hardware: HardwareIO, config: HardwareSafetyConfig
) -> GateFailure | None:
    """The DUT must report itself as MODE_BENCH before chaos runs.

    Defense in depth: the firmware also rejects chaos commands when not
    in bench mode (separate enforcement), but we double-check on the
    orchestrator side so a misconfigured-firmware DUT doesn't get
    silently abused.
    """
    info = await hardware.device_info()
    if info.mode != "BENCH":
        return GateFailure(
            AbortReason.CLUSTER_DENIED,
            f"DUT {info.serial!r} reports mode={info.mode!r}; chaos requires BENCH mode",
        )
    if config.expected_serial is not None and info.serial != config.expected_serial:
        return GateFailure(
            AbortReason.CLUSTER_DENIED,
            f"DUT serial {info.serial!r} does not match expected {config.expected_serial!r}; "
            "wrong device on the bench?",
        )
    return None


async def check_geofence(
    hardware: HardwareIO, config: HardwareSafetyConfig
) -> GateFailure | None:
    """The DUT must report the expected GPS/WiFi fingerprint.

    Skipped if `expected_geofence_tag` is None — for desk-side dev work
    where geofencing would just get in the way. In a real lab the
    operator pins the tag and the gate refuses to run an emission test
    from anywhere that isn't the lab.

    The DUT exposes its current fingerprint as a string-valued metric
    on the same telemetry endpoint. We compare exactly: a label drift
    fails closed.
    """
    if config.expected_geofence_tag is None:
        return None
    sample = await hardware.read_telemetry("geofence_tag")
    actual = sample.labels.get("tag") or sample.labels.get("value")
    if actual is None:
        return GateFailure(
            AbortReason.CLUSTER_DENIED,
            "DUT did not report a geofence_tag; refusing emission test outside a known location",
        )
    if actual != config.expected_geofence_tag:
        return GateFailure(
            AbortReason.CLUSTER_DENIED,
            f"DUT geofence_tag {actual!r} does not match expected "
            f"{config.expected_geofence_tag!r}; device left the lab?",
        )
    return None


async def check_thermal_headroom(
    hardware: HardwareIO, config: HardwareSafetyConfig
) -> GateFailure | None:
    """Refuse to start a run if the DUT is already running hot.

    Reads `die_temperature_c` from the telemetry endpoint. If it's
    above _THERMAL_MAX_C, abort: chaos that includes power cycles or
    sustained transmit duty would push silicon past spec.
    """
    sample = await hardware.read_telemetry("die_temperature_c")
    if sample.value > _THERMAL_MAX_C:
        return GateFailure(
            AbortReason.BLAST_RADIUS_VIOLATION,
            f"DUT die temp is {sample.value:.1f}°C (max {_THERMAL_MAX_C}°C); "
            "let it cool before running chaos",
        )
    return None


async def check_battery_headroom(
    hardware: HardwareIO, config: HardwareSafetyConfig
) -> GateFailure | None:
    """Refuse to start a run if the DUT battery is low.

    A brownout cascade on a battery sitting at 20% bricks the device.
    Reads `battery_soc` (state-of-charge, 0.0-1.0); below
    _BATTERY_MIN_SOC, abort.
    """
    sample = await hardware.read_telemetry("battery_soc")
    if sample.value < _BATTERY_MIN_SOC:
        return GateFailure(
            AbortReason.BLAST_RADIUS_VIOLATION,
            f"DUT battery SoC is {sample.value:.2f} (min {_BATTERY_MIN_SOC}); "
            "charge the DUT before running chaos",
        )
    return None


def check_emission_compliance(
    plan: ExperimentPlan, config: HardwareSafetyConfig
) -> GateFailure | None:
    """Every rf.* fault must stay within the licensed-band list.

    We read `channel` from each RF fault's parameters and verify it's
    inside at least one `licensed_bands` range. Channel 0 means "sweep"
    -- only permitted if the license covers the entire 1-14 ISM range
    (i.e., the operator is in a country whose regulator covers ch 1-14).

    Synchronous gate -- runs against the plan, no hardware roundtrip.
    """
    rf_faults = [f for f in plan.faults if f.category == "rf"]
    for fault in rf_faults:
        channel = int(fault.parameters.get("channel", 0))
        if channel == 0:
            # "sweep" — allowed only when the license covers ch 1..14.
            covers_ism = any(lo <= 1 and hi >= 14 for lo, hi in config.licensed_bands)
            if not covers_ism:
                return GateFailure(
                    AbortReason.CLUSTER_DENIED,
                    f"fault {fault.name!r} requests channel sweep but the bench is "
                    f"licensed only for {config.licensed_bands}; can't sweep without ISM coverage",
                )
            continue
        if not _channel_in_bands(channel, config.licensed_bands):
            return GateFailure(
                AbortReason.CLUSTER_DENIED,
                f"fault {fault.name!r} on channel {channel} is outside the licensed "
                f"bands {config.licensed_bands}; refuse to emit",
            )
    return None


# ---------------------------------------------------------------- runner


async def run_all_gates(
    plan: ExperimentPlan,
    hardware: HardwareIO,
    config: HardwareSafetyConfig,
) -> GateFailure | None:
    """Evaluate every gate; return the first failure (or None if all pass).

    Order: cheapest-first (in-process emission check, then DUT-side
    reads). Stops at first failure so a fail-closed gate doesn't get
    second-guessed by a later one.
    """
    if fail := check_emission_compliance(plan, config):
        return fail
    if fail := await check_bench_mode(hardware, config):
        return fail
    if fail := await check_geofence(hardware, config):
        return fail
    if fail := await check_thermal_headroom(hardware, config):
        return fail
    if fail := await check_battery_headroom(hardware, config):
        return fail
    return None


# ---------------------------------------------------------------- helpers


def _channel_in_bands(channel: int, bands: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= channel <= hi for lo, hi in bands)
