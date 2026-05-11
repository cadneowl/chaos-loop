"""
Cluster I/O — applying / observing / deleting Kubernetes resources.

The chaos agent depends on the ClusterIO Protocol, not on a concrete kubernetes
client. This keeps execute() unit-testable against FakeClusterIO without spinning
up a cluster, and leaves room for swapping in alternative implementations (e.g.,
remote agent-based execution).

Implementations:
    - FakeClusterIO: in-memory, records every call. Used by tests.
    - KubernetesClusterIO: stub. Lives in M3.c.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol


class ClusterIO(Protocol):
    """Minimal cluster interface used by the chaos agent."""

    async def apply(self, body: dict) -> dict:
        """Apply a CRD body. Returns the applied resource (with server-set fields)."""

    async def get(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> dict | None:
        """Read a CRD; None if not found."""

    async def delete(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> bool:
        """Delete a CRD. Returns True if it existed, False otherwise. Idempotent."""

    async def list_by_labels(
        self, api_version: str, kind: str, namespace: str, labels: dict[str, str]
    ) -> list[dict]:
        """List CRDs in `namespace` matching every label in `labels`."""


# ---------------------------------------------------------------------------- #
# Fake cluster (tests)                                                         #
# ---------------------------------------------------------------------------- #


@dataclass
class FakeClusterIO:
    """In-memory cluster. Records apply / delete calls for assertions."""

    # (apiVersion, kind, namespace, name) -> body
    resources: dict[tuple[str, str, str, str], dict] = field(default_factory=dict)
    applied: list[dict] = field(default_factory=list)
    deleted: list[tuple[str, str, str, str]] = field(default_factory=list)

    # Hook to inject behavior in tests, e.g. raise on apply once.
    apply_hook: Callable[[dict], Awaitable[None]] | None = None

    async def apply(self, body: dict) -> dict:
        if self.apply_hook is not None:
            await self.apply_hook(body)
        body = copy.deepcopy(body)
        # Mimic server-set fields: a default Running status.
        body.setdefault("status", {"phase": "Running", "conditions": [{"type": "AllInjected"}]})
        key = (
            body["apiVersion"],
            body["kind"],
            body["metadata"]["namespace"],
            body["metadata"]["name"],
        )
        self.resources[key] = body
        self.applied.append(body)
        return body

    async def get(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> dict | None:
        return self.resources.get((api_version, kind, namespace, name))

    async def delete(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> bool:
        key = (api_version, kind, namespace, name)
        existed = key in self.resources
        self.resources.pop(key, None)
        self.deleted.append(key)
        return existed

    async def list_by_labels(
        self, api_version: str, kind: str, namespace: str, labels: dict[str, str]
    ) -> list[dict]:
        out: list[dict] = []
        for key, r in self.resources.items():
            if key[0] != api_version or key[1] != kind or key[2] != namespace:
                continue
            res_labels = r.get("metadata", {}).get("labels", {})
            if all(res_labels.get(k) == v for k, v in labels.items()):
                out.append(r)
        return out
