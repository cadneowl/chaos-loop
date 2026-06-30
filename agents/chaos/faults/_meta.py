"""
Declarative catalogue of every fault this system knows how to inject.

Adding a new fault: append an entry here, write a renderer function (later
milestone), and document it in docs/SECURITY_CHAOS.md if it's security-flavored.

The orchestrator reads this catalogue when validating ExperimentPlans.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.contracts import FaultCategory


@dataclass(frozen=True)
class FaultDef:
    name: str
    category: FaultCategory
    description: str
    requires_approval: bool
    chaos_mesh_kind: str | None  # None means custom (not a native CRD)


CATALOGUE: dict[str, FaultDef] = {
    # ---- hardware (HardwareChaosAgent path; chaos_mesh_kind is None because
    # the fault is enacted by an attack device over serial/MQTT, not via the
    # Kubernetes API).
    "wifi.deauth": FaultDef(
        name="wifi.deauth",
        category=FaultCategory.RF,
        description=(
            "Broadcast 802.11 deauthentication frames at the device-under-test's "
            "BSSID, simulating a low-cost WiFi jammer. Used to verify NeoOwl's "
            "detection latency and mesh-failover behavior."
        ),
        requires_approval=False,
        chaos_mesh_kind=None,
    ),
    "wifi.jam": FaultDef(
        name="wifi.jam",
        category=FaultCategory.RF,
        description=(
            "Sweep a noise carrier across the 2.4 GHz ISM band. Indiscriminate "
            "(blocks all WiFi traffic, not just the DUT's link) — verifies the "
            "device degrades cleanly to LoRa/cellular fallback rather than "
            "hanging on a dead WiFi association."
        ),
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    "ble.advertising_flood": FaultDef(
        name="ble.advertising_flood",
        category=FaultCategory.RF,
        description=(
            "Emit BLE advertising packets at ~10k/sec from N spoofed MAC "
            "addresses. Used to verify the BLE scanner's queue + dedup logic "
            "and that CPU headroom holds under adversarial discovery noise."
        ),
        requires_approval=False,
        chaos_mesh_kind=None,
    ),
    "lora.jam": FaultDef(
        name="lora.jam",
        category=FaultCategory.RF,
        description=(
            "Continuous unmodulated carrier on the DUT's LoRa channel. "
            "Used to verify gateway-side packet-loss reporting and that "
            "the NVR's store-and-forward buffer absorbs the resulting "
            "uplink stall without dropping security events."
        ),
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    # ---- power.* (HardwareChaosAgent path; programmable bench PSU drives the
    # DUT's supply rail). Phase 3 of the NeoOwl adaptation.
    "power.brownout": FaultDef(
        name="power.brownout",
        category=FaultCategory.POWER,
        description=(
            "Drop the DUT's supply rail to a configurable millivolt floor "
            "for `duration_seconds`. Used to verify the brownout detector "
            "fires, NVS writes are journaled, and the device recovers "
            "without bricking. Repeated brownouts surface latent boot loops."
        ),
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    "power.ramp": FaultDef(
        name="power.ramp",
        category=FaultCategory.POWER,
        description=(
            "Slow ramp the supply rail from a configurable start to floor "
            "voltage over `duration_seconds`. Tests the brownout detector's "
            "hysteresis behavior — should fire well before the rail reaches "
            "the firmware's minimum operating voltage."
        ),
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    "power.cut": FaultDef(
        name="power.cut",
        category=FaultCategory.POWER,
        description=(
            "Hard supply cut for `duration_seconds`, then restore. "
            "Validates the capacitor-backed event buffer: any security "
            "event captured in the second before the cut must survive "
            "the power-cycle and arrive at the gateway on next boot."
        ),
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    # ---- sensor.* (HardwareChaosAgent path; an inline I²C/SPI mux disconnects
    # the sensor's bus or replays a frozen reading). Phase 3.
    "sensor.dropout": FaultDef(
        name="sensor.dropout",
        category=FaultCategory.SENSOR,
        description=(
            "Disconnect a sensor's I²C/SPI bus for `duration_seconds` so "
            "the firmware sees a missing peripheral. Used to verify mesh "
            "consensus degrades gracefully (the device must signal "
            "degradation rather than silently dropping the sensor)."
        ),
        requires_approval=False,
        chaos_mesh_kind=None,
    ),
    "sensor.stuck": FaultDef(
        name="sensor.stuck",
        category=FaultCategory.SENSOR,
        description=(
            "Intercept a sensor's bus and replay the last observed reading "
            "for `duration_seconds`. The reading looks plausible to a "
            "single-sensor check; the anomaly detector should catch the "
            "flatline against neighbor sensors."
        ),
        requires_approval=False,
        chaos_mesh_kind=None,
    ),
    # ---- time.* hardware-side (gateway firewall blocks NTP or supplies a
    # skewed server). Reuses FaultCategory.TIME but with chaos_mesh_kind=None
    # so HardwareChaosAgent picks them up rather than the Chaos Mesh path.
    "time.ntp.cut": FaultDef(
        name="time.ntp.cut",
        category=FaultCategory.TIME,
        description=(
            "Firewall NTP traffic at the gateway for `duration_seconds`. "
            "Verifies the gateway's clock-source fallback (PPS / cellular "
            "modem time) and that cert-renewal scheduling tolerates the "
            "drift without deferring critical renewals."
        ),
        requires_approval=False,
        chaos_mesh_kind=None,
    ),
    "time.clock.drift": FaultDef(
        name="time.clock.drift",
        category=FaultCategory.TIME,
        description=(
            "Inject a fake NTP server with a configurable skew in seconds. "
            "Validates that cert validation refuses to accept now-future "
            "or now-past certificates regardless of what the system clock "
            "claims."
        ),
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    # ---- classical (Chaos Mesh native) ----
    "pod.kill": FaultDef(
        name="pod.kill",
        category=FaultCategory.POD,
        description="Kill one or more pods matching a selector.",
        requires_approval=False,
        chaos_mesh_kind="PodChaos",
    ),
    "pod.failure": FaultDef(
        name="pod.failure",
        category=FaultCategory.POD,
        description="Continuously crash a pod for the duration.",
        requires_approval=False,
        chaos_mesh_kind="PodChaos",
    ),
    "network.loss": FaultDef(
        name="network.loss",
        category=FaultCategory.NETWORK,
        description="Packet loss between source pods and a target.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
    ),
    "network.delay": FaultDef(
        name="network.delay",
        category=FaultCategory.NETWORK,
        description="Inject latency on egress from source pods to a target.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
    ),
    "network.partition": FaultDef(
        name="network.partition",
        category=FaultCategory.NETWORK,
        description="Drop all traffic between two sets of pods (split-brain).",
        requires_approval=True,
        chaos_mesh_kind="NetworkChaos",
    ),
    "io.latency": FaultDef(
        name="io.latency",
        category=FaultCategory.IO,
        description="Delay file system I/O on a mount.",
        requires_approval=False,
        chaos_mesh_kind="IOChaos",
    ),
    "stress.cpu": FaultDef(
        name="stress.cpu",
        category=FaultCategory.STRESS,
        description="Saturate CPU on target pods.",
        requires_approval=False,
        chaos_mesh_kind="StressChaos",
    ),
    "stress.memory": FaultDef(
        name="stress.memory",
        category=FaultCategory.STRESS,
        description="Consume memory on target pods (may trigger OOM).",
        requires_approval=True,
        chaos_mesh_kind="StressChaos",
    ),
    "dns.error": FaultDef(
        name="dns.error",
        category=FaultCategory.DNS,
        description="DNS resolution failures for a domain pattern.",
        requires_approval=False,
        chaos_mesh_kind="DNSChaos",
    ),
    "http.abort": FaultDef(
        name="http.abort",
        category=FaultCategory.HTTP,
        description="Abort matching HTTP requests at the proxy.",
        requires_approval=False,
        chaos_mesh_kind="HTTPChaos",
    ),
    "time.skew": FaultDef(
        name="time.skew",
        category=FaultCategory.TIME,
        description="Shift the clock inside target pods.",
        requires_approval=False,
        chaos_mesh_kind="TimeChaos",
    ),
    # ---- security-flavored ----
    "cert.revoke": FaultDef(
        name="cert.revoke",
        category=FaultCategory.CERT,
        description="Block OCSP/CRL endpoints + simulate cert revocation.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
    ),
    "cert.expire": FaultDef(
        name="cert.expire",
        category=FaultCategory.CERT,
        description="Time-skew target pods past cert NotAfter.",
        requires_approval=False,
        chaos_mesh_kind="TimeChaos",
    ),
    "tls.downgrade": FaultDef(
        name="tls.downgrade",
        category=FaultCategory.TLS,
        description="Force TLS 1.0 / plaintext via proxy rewrites; expect refusal.",
        requires_approval=True,
        chaos_mesh_kind="HTTPChaos",
    ),
    "auth.outage": FaultDef(
        name="auth.outage",
        category=FaultCategory.AUTH,
        description="Block egress to the IdP; expect fail-closed behavior.",
        requires_approval=True,
        chaos_mesh_kind="NetworkChaos",
    ),
    "auth.latency": FaultDef(
        name="auth.latency",
        category=FaultCategory.AUTH,
        description="Inject latency on the IdP path; expect no side-channels.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
    ),
    "secret.rotate": FaultDef(
        name="secret.rotate",
        category=FaultCategory.SECRET,
        description="Patch a Secret mid-flight; expect graceful reload, no leak.",
        requires_approval=False,
        chaos_mesh_kind=None,
    ),
    "image.swap_vuln": FaultDef(
        name="image.swap_vuln",
        category=FaultCategory.IMAGE,
        description="Deploy a known-vulnerable image; expect admission rejection.",
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    "image.swap_unsigned": FaultDef(
        name="image.swap_unsigned",
        category=FaultCategory.IMAGE,
        description="Deploy an unsigned image; expect admission rejection (cosign).",
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    "iam.degrade": FaultDef(
        name="iam.degrade",
        category=FaultCategory.IAM,
        description="Partial loss on STS / cloud IAM endpoints.",
        requires_approval=True,
        chaos_mesh_kind="NetworkChaos",
    ),
    "netpol.regress": FaultDef(
        name="netpol.regress",
        category=FaultCategory.NETPOL,
        description="Remove a NetworkPolicy; expect app-layer enforcement to hold.",
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    "egress.exfil_sim": FaultDef(
        name="egress.exfil_sim",
        category=FaultCategory.EGRESS,
        description="Curl to controlled sink resembling exfil; expect runtime alert.",
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
    "runtime.tamper": FaultDef(
        name="runtime.tamper",
        category=FaultCategory.RUNTIME,
        description="Write to rootfs in a running container; expect runtime alert.",
        requires_approval=True,
        chaos_mesh_kind=None,
    ),
}


def fault_names() -> list[str]:
    return sorted(CATALOGUE.keys())


def by_category(cat: FaultCategory) -> list[FaultDef]:
    return [f for f in CATALOGUE.values() if f.category == cat]
