"""NetworkChaos renderers — network.loss, network.delay, network.partition.

Reference: https://chaos-mesh.org/docs/simulate-network-chaos-on-kubernetes/
"""

from __future__ import annotations

from agents.chaos.faults._render import (
    RenderContext,
    _duration,
    _metadata,
    _selector,
)
from shared.contracts import FaultSpec


def _direction(fault: FaultSpec) -> str:
    """to | from | both. Chaos Mesh defaults to 'to' (egress)."""
    return str(fault.parameters.get("direction", "to"))


def render_network_loss(fault: FaultSpec, ctx: RenderContext) -> dict:
    """Drop a percentage of packets between source pods and a target."""
    pct = fault.parameters.get("loss_percent", 100)
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "NetworkChaos",
        "metadata": _metadata(fault, ctx),
        "spec": {
            "action": "loss",
            "mode": fault.parameters.get("mode", "all"),
            "selector": _selector(fault, ctx),
            "direction": _direction(fault),
            "loss": {
                # Chaos Mesh expects strings here, not numbers.
                "loss": str(pct),
                "correlation": str(fault.parameters.get("correlation", "0")),
            },
            "duration": _duration(fault),
        },
    }


def render_network_delay(fault: FaultSpec, ctx: RenderContext) -> dict:
    """Inject latency on egress from source pods to a target."""
    latency = fault.parameters.get("latency_ms", 100)
    jitter = fault.parameters.get("jitter_ms", 0)
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "NetworkChaos",
        "metadata": _metadata(fault, ctx),
        "spec": {
            "action": "delay",
            "mode": fault.parameters.get("mode", "all"),
            "selector": _selector(fault, ctx),
            "direction": _direction(fault),
            "delay": {
                "latency": f"{latency}ms",
                "jitter": f"{jitter}ms",
                "correlation": str(fault.parameters.get("correlation", "0")),
            },
            "duration": _duration(fault),
        },
    }


def render_network_partition(fault: FaultSpec, ctx: RenderContext) -> dict:
    """
    Drop all traffic between two sets of pods.

    Expects `target_selector_other` in parameters: the labels of the OTHER side
    of the partition. We use Chaos Mesh's `target` field to express this.
    """
    other = fault.parameters.get("target_selector_other") or {}
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "NetworkChaos",
        "metadata": _metadata(fault, ctx),
        "spec": {
            "action": "partition",
            "mode": fault.parameters.get("mode", "all"),
            "selector": _selector(fault, ctx),
            "direction": "both",  # partition is symmetric by definition
            "target": {
                "mode": "all",
                "selector": {
                    "namespaces": [ctx.namespace],
                    "labelSelectors": dict(other),
                },
            },
            "duration": _duration(fault),
        },
    }
