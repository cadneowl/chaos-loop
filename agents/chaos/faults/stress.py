"""StressChaos renderers — stress.cpu, stress.memory.

Reference: https://chaos-mesh.org/docs/simulate-heavy-stress-on-kubernetes/
"""

from __future__ import annotations

from agents.chaos.faults._render import (
    RenderContext,
    _duration,
    _metadata,
    _selector,
)
from shared.contracts import FaultSpec


def render_stress_cpu(fault: FaultSpec, ctx: RenderContext) -> dict:
    """Saturate CPU. `workers` = number of stressor processes; `load` = % per worker."""
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "StressChaos",
        "metadata": _metadata(fault, ctx),
        "spec": {
            "mode": fault.parameters.get("mode", "one"),
            "selector": _selector(fault, ctx),
            "stressors": {
                "cpu": {
                    "workers": int(fault.parameters.get("workers", 1)),
                    "load": int(fault.parameters.get("load_percent", 100)),
                },
            },
            "duration": _duration(fault),
        },
    }


def render_stress_memory(fault: FaultSpec, ctx: RenderContext) -> dict:
    """Consume memory. `size` is the amount each worker tries to grab.

    Warning: at high `size` values this can OOM-kill the target pod, which is
    sometimes the point (test OOM behavior) and sometimes unwanted noise.
    """
    size = fault.parameters.get("size", "256MB")
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "StressChaos",
        "metadata": _metadata(fault, ctx),
        "spec": {
            "mode": fault.parameters.get("mode", "one"),
            "selector": _selector(fault, ctx),
            "stressors": {
                "memory": {
                    "workers": int(fault.parameters.get("workers", 1)),
                    "size": str(size),
                },
            },
            "duration": _duration(fault),
        },
    }
