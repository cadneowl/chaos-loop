"""
Registry of fault-name -> renderer function.

The chaos agent looks up the renderer here; it does NOT import individual renderer
modules directly. New fault types: add an entry here and provide the renderer.
"""

from __future__ import annotations

from collections.abc import Callable

from agents.chaos.faults import network, pod, stress
from agents.chaos.faults._meta import CATALOGUE
from agents.chaos.faults._render import RenderContext
from shared.contracts import FaultSpec

Renderer = Callable[[FaultSpec, RenderContext], dict]


RENDERERS: dict[str, Renderer] = {
    # Pod
    "pod.kill": pod.render_pod_kill,
    "pod.failure": pod.render_pod_failure,
    # Network
    "network.loss": network.render_network_loss,
    "network.delay": network.render_network_delay,
    "network.partition": network.render_network_partition,
    # Stress
    "stress.cpu": stress.render_stress_cpu,
    "stress.memory": stress.render_stress_memory,
}


def has_renderer(fault_name: str) -> bool:
    return fault_name in RENDERERS


def render(fault: FaultSpec, ctx: RenderContext) -> dict:
    """Render a FaultSpec to a CRD body. Raises KeyError for unrenderable faults."""
    if fault.name not in CATALOGUE:
        raise KeyError(f"fault {fault.name!r} not in catalogue")
    renderer = RENDERERS.get(fault.name)
    if renderer is None:
        raise KeyError(
            f"fault {fault.name!r} is in catalogue but has no renderer "
            f"(milestone-3 covers {sorted(RENDERERS)})"
        )
    return renderer(fault, ctx)


def unrendered_faults() -> list[str]:
    """Faults that exist in the catalogue but don't yet have a renderer (M3 progress tracker)."""
    return sorted(set(CATALOGUE) - set(RENDERERS))
