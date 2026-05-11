"""
Common helpers for fault renderers.

Each renderer is a pure function `(FaultSpec, RenderContext) -> dict` that produces
a Chaos Mesh CRD body. No I/O. No randomness. Same input -> same output.

This is deliberately decoupled from kubernetes client calls; applying the CRD is
a separate step owned by the chaos agent's ClusterIO.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.contracts import FaultSpec


@dataclass(frozen=True)
class RenderContext:
    """Cluster-level context every renderer needs: which namespace, which experiment."""

    namespace: str
    experiment_id: str


def _resource_name(fault: FaultSpec, ctx: RenderContext) -> str:
    """Stable, sub-63-char DNS-safe name for the CRD.

    Format: <fault-name-slug>-<exp-suffix>. We use the last 8 chars of the
    experiment_id (it's already `exp-<12 hex>`, so 8 chars is plenty unique).
    """
    slug = fault.name.replace(".", "-").replace("_", "-").lower()
    return f"{slug}-{ctx.experiment_id[-8:]}"


def _common_labels(fault: FaultSpec, ctx: RenderContext) -> dict[str, str]:
    """Labels we always apply, so cleanup can find every CRD belonging to a run."""
    return {
        "chaos.kosta.dev/experiment-id": ctx.experiment_id,
        "chaos.kosta.dev/fault-name": fault.name,
        "chaos.kosta.dev/fault-category": fault.category.value,
    }


def _metadata(fault: FaultSpec, ctx: RenderContext) -> dict:
    return {
        "name": _resource_name(fault, ctx),
        "namespace": ctx.namespace,
        "labels": _common_labels(fault, ctx),
        "annotations": {
            "chaos.kosta.dev/rationale": fault.rationale,
        },
    }


def _duration(fault: FaultSpec) -> str:
    """Chaos Mesh accepts Go duration strings (e.g., '60s', '5m')."""
    return f"{fault.duration_seconds}s"


def _selector(fault: FaultSpec, ctx: RenderContext) -> dict:
    """Standard Chaos Mesh selector: namespace + labelSelectors from the FaultSpec."""
    return {
        "namespaces": [ctx.namespace],
        "labelSelectors": dict(fault.target_selector),
    }
