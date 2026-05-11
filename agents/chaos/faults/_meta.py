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
    docs_anchor: str  # heading in SECURITY_CHAOS.md or ARCHITECTURE.md


CATALOGUE: dict[str, FaultDef] = {
    # ---- classical (Chaos Mesh native) ----
    "pod.kill": FaultDef(
        name="pod.kill",
        category=FaultCategory.POD,
        description="Kill one or more pods matching a selector.",
        requires_approval=False,
        chaos_mesh_kind="PodChaos",
        docs_anchor="pod-faults",
    ),
    "pod.failure": FaultDef(
        name="pod.failure",
        category=FaultCategory.POD,
        description="Continuously crash a pod for the duration.",
        requires_approval=False,
        chaos_mesh_kind="PodChaos",
        docs_anchor="pod-faults",
    ),
    "network.loss": FaultDef(
        name="network.loss",
        category=FaultCategory.NETWORK,
        description="Packet loss between source pods and a target.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
        docs_anchor="network-faults",
    ),
    "network.delay": FaultDef(
        name="network.delay",
        category=FaultCategory.NETWORK,
        description="Inject latency on egress from source pods to a target.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
        docs_anchor="network-faults",
    ),
    "network.partition": FaultDef(
        name="network.partition",
        category=FaultCategory.NETWORK,
        description="Drop all traffic between two sets of pods (split-brain).",
        requires_approval=True,
        chaos_mesh_kind="NetworkChaos",
        docs_anchor="network-faults",
    ),
    "io.latency": FaultDef(
        name="io.latency",
        category=FaultCategory.IO,
        description="Delay file system I/O on a mount.",
        requires_approval=False,
        chaos_mesh_kind="IOChaos",
        docs_anchor="io-faults",
    ),
    "stress.cpu": FaultDef(
        name="stress.cpu",
        category=FaultCategory.STRESS,
        description="Saturate CPU on target pods.",
        requires_approval=False,
        chaos_mesh_kind="StressChaos",
        docs_anchor="stress-faults",
    ),
    "stress.memory": FaultDef(
        name="stress.memory",
        category=FaultCategory.STRESS,
        description="Consume memory on target pods (may trigger OOM).",
        requires_approval=True,
        chaos_mesh_kind="StressChaos",
        docs_anchor="stress-faults",
    ),
    "dns.error": FaultDef(
        name="dns.error",
        category=FaultCategory.DNS,
        description="DNS resolution failures for a domain pattern.",
        requires_approval=False,
        chaos_mesh_kind="DNSChaos",
        docs_anchor="dns-faults",
    ),
    "http.abort": FaultDef(
        name="http.abort",
        category=FaultCategory.HTTP,
        description="Abort matching HTTP requests at the proxy.",
        requires_approval=False,
        chaos_mesh_kind="HTTPChaos",
        docs_anchor="http-faults",
    ),
    "time.skew": FaultDef(
        name="time.skew",
        category=FaultCategory.TIME,
        description="Shift the clock inside target pods.",
        requires_approval=False,
        chaos_mesh_kind="TimeChaos",
        docs_anchor="time-faults",
    ),
    # ---- security-flavored ----
    "cert.revoke": FaultDef(
        name="cert.revoke",
        category=FaultCategory.CERT,
        description="Block OCSP/CRL endpoints + simulate cert revocation.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
        docs_anchor="security-faults",
    ),
    "cert.expire": FaultDef(
        name="cert.expire",
        category=FaultCategory.CERT,
        description="Time-skew target pods past cert NotAfter.",
        requires_approval=False,
        chaos_mesh_kind="TimeChaos",
        docs_anchor="security-faults",
    ),
    "tls.downgrade": FaultDef(
        name="tls.downgrade",
        category=FaultCategory.TLS,
        description="Force TLS 1.0 / plaintext via proxy rewrites; expect refusal.",
        requires_approval=True,
        chaos_mesh_kind="HTTPChaos",
        docs_anchor="security-faults",
    ),
    "auth.outage": FaultDef(
        name="auth.outage",
        category=FaultCategory.AUTH,
        description="Block egress to the IdP; expect fail-closed behavior.",
        requires_approval=True,
        chaos_mesh_kind="NetworkChaos",
        docs_anchor="security-faults",
    ),
    "auth.latency": FaultDef(
        name="auth.latency",
        category=FaultCategory.AUTH,
        description="Inject latency on the IdP path; expect no side-channels.",
        requires_approval=False,
        chaos_mesh_kind="NetworkChaos",
        docs_anchor="security-faults",
    ),
    "secret.rotate": FaultDef(
        name="secret.rotate",
        category=FaultCategory.SECRET,
        description="Patch a Secret mid-flight; expect graceful reload, no leak.",
        requires_approval=False,
        chaos_mesh_kind=None,
        docs_anchor="security-faults",
    ),
    "image.swap_vuln": FaultDef(
        name="image.swap_vuln",
        category=FaultCategory.IMAGE,
        description="Deploy a known-vulnerable image; expect admission rejection.",
        requires_approval=True,
        chaos_mesh_kind=None,
        docs_anchor="security-faults",
    ),
    "image.swap_unsigned": FaultDef(
        name="image.swap_unsigned",
        category=FaultCategory.IMAGE,
        description="Deploy an unsigned image; expect admission rejection (cosign).",
        requires_approval=True,
        chaos_mesh_kind=None,
        docs_anchor="security-faults",
    ),
    "iam.degrade": FaultDef(
        name="iam.degrade",
        category=FaultCategory.IAM,
        description="Partial loss on STS / cloud IAM endpoints.",
        requires_approval=True,
        chaos_mesh_kind="NetworkChaos",
        docs_anchor="security-faults",
    ),
    "netpol.regress": FaultDef(
        name="netpol.regress",
        category=FaultCategory.NETPOL,
        description="Remove a NetworkPolicy; expect app-layer enforcement to hold.",
        requires_approval=True,
        chaos_mesh_kind=None,
        docs_anchor="security-faults",
    ),
    "egress.exfil_sim": FaultDef(
        name="egress.exfil_sim",
        category=FaultCategory.EGRESS,
        description="Curl to controlled sink resembling exfil; expect runtime alert.",
        requires_approval=True,
        chaos_mesh_kind=None,
        docs_anchor="security-faults",
    ),
    "runtime.tamper": FaultDef(
        name="runtime.tamper",
        category=FaultCategory.RUNTIME,
        description="Write to rootfs in a running container; expect runtime alert.",
        requires_approval=True,
        chaos_mesh_kind=None,
        docs_anchor="security-faults",
    ),
}


def fault_names() -> list[str]:
    return sorted(CATALOGUE.keys())


def by_category(cat: FaultCategory) -> list[FaultDef]:
    return [f for f in CATALOGUE.values() if f.category == cat]
