"""
Cluster I/O — applying / observing / deleting Kubernetes resources.

The chaos agent depends on the ClusterIO Protocol, not on a concrete kubernetes
client. This keeps execute() unit-testable against FakeClusterIO without spinning
up a cluster, and leaves room for swapping in alternative implementations (e.g.,
remote agent-based execution).

Implementations:
    - FakeClusterIO: in-memory, records every call. Used by tests.
    - KubernetesClusterIO: real impl backed by the official kubernetes client,
      wrapping its sync calls in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ClusterIO(Protocol):
    """Minimal cluster interface used by the chaos agent + safety gates."""

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

    async def get_namespace_annotations(self, namespace: str) -> dict[str, str] | None:
        """Return the annotations on `namespace`. None means unreachable or missing."""


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

    # namespace name -> annotations dict. None entries simulate "unreachable".
    namespace_annotations: dict[str, dict[str, str] | None] = field(default_factory=dict)

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

    async def get_namespace_annotations(self, namespace: str) -> dict[str, str] | None:
        # By default a namespace is allowed for testing — tests that need to
        # exercise the gate can override via `namespace_annotations`.
        if namespace in self.namespace_annotations:
            return self.namespace_annotations[namespace]
        return {"chaos.kosta.dev/allowed": "true"}


# ---------------------------------------------------------------------------- #
# Kubernetes-backed cluster                                                    #
# ---------------------------------------------------------------------------- #


def _split_api_version(api_version: str) -> tuple[str, str]:
    """``chaos-mesh.org/v1alpha1`` -> ('chaos-mesh.org', 'v1alpha1').

    Built-in core resources (api_version=``v1``) aren't part of our chaos
    surface — every Chaos Mesh kind has a group, so we don't handle the
    core case.
    """
    if "/" not in api_version:
        raise ValueError(f"apiVersion {api_version!r} has no group; core resources unsupported")
    group, version = api_version.split("/", 1)
    return group, version


def _plural(kind: str) -> str:
    """Chaos Mesh CRDs all use ``kind.lower()`` as plural (PodChaos -> podchaos)."""
    return kind.lower()


class KubernetesClusterIO:
    """Real ClusterIO backed by the ``kubernetes`` sync client.

    Sync calls are wrapped in ``asyncio.to_thread`` so we don't block the loop.
    The kube clients are loaded once at construction (lazy, thread-safe);
    ``kubeconfig=None`` uses in-cluster config if available, else the default
    ``~/.kube/config``.

    Resource path: chaos faults all live as group/version namespaced custom
    resources, so we use ``CustomObjectsApi`` for everything chaos-related.
    The namespace-annotation lookup uses ``CoreV1Api`` and is the only
    built-in-resource call we make.
    """

    def __init__(
        self,
        *,
        kubeconfig: str | None = None,
        context: str | None = None,
    ) -> None:
        self.kubeconfig = kubeconfig
        self.context = context
        # Lazy loaders. The lock serializes the load — two concurrent
        # to_thread() callers must not both mutate kubernetes global config.
        self._custom_api: Any | None = None
        self._core_api: Any | None = None
        self._init_lock = Lock()

    def _ensure_loaded(self) -> None:
        """Load kubeconfig + construct both API clients exactly once."""
        if self._custom_api is not None and self._core_api is not None:
            return
        # Lazy import keeps `import agents.chaos.cluster` light when only
        # FakeClusterIO is used.
        from kubernetes import client, config

        with self._init_lock:
            # Re-check under the lock — another caller may have raced ahead.
            if self._custom_api is not None and self._core_api is not None:
                return
            try:
                config.load_incluster_config()
                log.info("KubernetesClusterIO: using in-cluster config")
            except config.ConfigException:
                config.load_kube_config(config_file=self.kubeconfig, context=self.context)
                log.info(
                    "KubernetesClusterIO: loaded kubeconfig (%s, context=%s)",
                    self.kubeconfig or "default",
                    self.context or "current",
                )
            self._custom_api = client.CustomObjectsApi()
            self._core_api = client.CoreV1Api()

    def _api(self) -> Any:
        self._ensure_loaded()
        return self._custom_api

    def _core(self) -> Any:
        self._ensure_loaded()
        return self._core_api

    async def get_namespace_annotations(self, namespace: str) -> dict[str, str] | None:
        """Return annotations on the named namespace, or None if unreachable.

        Used by the orchestrator's namespace-annotation safety gate. We return
        None (not raise) on connectivity errors so the gate can fail closed
        with a clear message instead of leaking a stack trace.
        """
        core = self._core()

        def _do() -> dict[str, str] | None:
            from kubernetes.client.exceptions import ApiException

            try:
                ns = core.read_namespace(name=namespace)
            except ApiException as e:
                if e.status == 404:
                    return None
                raise
            return dict(ns.metadata.annotations or {})

        try:
            return await asyncio.to_thread(_do)
        except Exception as e:  # connection refused, DNS, cert, etc.
            log.warning(
                "get_namespace_annotations(%r) failed: %r", namespace, e
            )
            return None

    async def apply(self, body: dict) -> dict:
        """Create-or-replace by name. We don't use server-side apply (kubectl's
        ``--field-manager`` path) because every Chaos Mesh resource we create
        is owned exclusively by this experiment — name conflicts mean a stale
        leftover, and replacing is the right behavior."""
        group, version = _split_api_version(body["apiVersion"])
        plural = _plural(body["kind"])
        name = body["metadata"]["name"]
        namespace = body["metadata"]["namespace"]

        api = self._api()

        def _do() -> dict:
            from kubernetes.client.exceptions import ApiException

            try:
                return api.create_namespaced_custom_object(
                    group=group,
                    version=version,
                    namespace=namespace,
                    plural=plural,
                    body=body,
                )
            except ApiException as e:
                if e.status != 409:  # Conflict — exists; replace it.
                    raise
                existing = api.get_namespaced_custom_object(
                    group=group, version=version, namespace=namespace,
                    plural=plural, name=name,
                )
                # Preserve resourceVersion for the replace; everything else
                # comes from the new body.
                merged = copy.deepcopy(body)
                merged["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
                return api.replace_namespaced_custom_object(
                    group=group, version=version, namespace=namespace,
                    plural=plural, name=name, body=merged,
                )

        return await asyncio.to_thread(_do)

    async def get(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> dict | None:
        group, version = _split_api_version(api_version)
        plural = _plural(kind)
        api = self._api()

        def _do() -> dict | None:
            from kubernetes.client.exceptions import ApiException

            try:
                return api.get_namespaced_custom_object(
                    group=group, version=version, namespace=namespace,
                    plural=plural, name=name,
                )
            except ApiException as e:
                if e.status == 404:
                    return None
                raise

        return await asyncio.to_thread(_do)

    async def delete(
        self, api_version: str, kind: str, name: str, namespace: str
    ) -> bool:
        """Idempotent: 404 returns False, success returns True."""
        group, version = _split_api_version(api_version)
        plural = _plural(kind)
        api = self._api()

        def _do() -> bool:
            from kubernetes.client.exceptions import ApiException

            try:
                api.delete_namespaced_custom_object(
                    group=group, version=version, namespace=namespace,
                    plural=plural, name=name,
                )
                return True
            except ApiException as e:
                if e.status == 404:
                    return False
                raise

        return await asyncio.to_thread(_do)

    async def list_by_labels(
        self, api_version: str, kind: str, namespace: str, labels: dict[str, str]
    ) -> list[dict]:
        group, version = _split_api_version(api_version)
        plural = _plural(kind)
        api = self._api()
        # Server-side label filter — `,`-joined for AND semantics.
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

        def _do() -> list[dict]:
            from kubernetes.client.exceptions import ApiException

            try:
                resp = api.list_namespaced_custom_object(
                    group=group, version=version, namespace=namespace,
                    plural=plural, label_selector=selector,
                )
            except ApiException as e:
                # 404 on the resource type itself means "Chaos Mesh isn't
                # installed / this kind doesn't exist" — return empty rather
                # than raise, mirroring FakeClusterIO's behavior on a missing
                # kind.
                if e.status == 404:
                    return []
                raise
            return list(resp.get("items", []))

        return await asyncio.to_thread(_do)
