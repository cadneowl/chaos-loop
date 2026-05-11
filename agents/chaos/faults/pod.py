"""PodChaos renderers — pod.kill, pod.failure.

Reference: https://chaos-mesh.org/docs/simulate-pod-chaos-on-kubernetes/
"""

from __future__ import annotations

from agents.chaos.faults._render import (
    RenderContext,
    _duration,
    _metadata,
    _selector,
)
from shared.contracts import FaultSpec


def render_pod_kill(fault: FaultSpec, ctx: RenderContext) -> dict:
    """One-shot pod-kill. Kills matching pods; they restart per their controller's policy."""
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "PodChaos",
        "metadata": _metadata(fault, ctx),
        "spec": {
            "action": "pod-kill",
            "mode": fault.parameters.get("mode", "one"),
            "selector": _selector(fault, ctx),
            # pod-kill doesn't take duration (it's instantaneous) but Chaos Mesh
            # uses gracePeriod to defer the kill signal.
            "gracePeriod": int(fault.parameters.get("grace_period_seconds", 0)),
        },
    }


def render_pod_failure(fault: FaultSpec, ctx: RenderContext) -> dict:
    """Continuous pod failure for `duration`. Containers crashloop during the window."""
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "PodChaos",
        "metadata": _metadata(fault, ctx),
        "spec": {
            "action": "pod-failure",
            "mode": fault.parameters.get("mode", "one"),
            "selector": _selector(fault, ctx),
            "duration": _duration(fault),
        },
    }
