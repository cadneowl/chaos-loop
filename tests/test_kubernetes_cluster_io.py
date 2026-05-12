"""Tests for KubernetesClusterIO — the real ClusterIO backed by the
kubernetes client. The kubernetes client itself is mocked so these tests
don't need a cluster; a live round-trip lives in
``scripts/validate_renderers.py`` and the M3.c smoke test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.chaos.cluster import KubernetesClusterIO, _plural, _split_api_version

# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


def test_split_api_version_extracts_group_and_version() -> None:
    assert _split_api_version("chaos-mesh.org/v1alpha1") == ("chaos-mesh.org", "v1alpha1")


def test_split_api_version_rejects_core_resources() -> None:
    with pytest.raises(ValueError, match="no group"):
        _split_api_version("v1")


@pytest.mark.parametrize(
    "kind,plural",
    [
        ("PodChaos", "podchaos"),
        ("NetworkChaos", "networkchaos"),
        ("StressChaos", "stresschaos"),
        ("HTTPChaos", "httpchaos"),
    ],
)
def test_plural_lowercases_kind(kind: str, plural: str) -> None:
    assert _plural(kind) == plural


# --------------------------------------------------------------------------- #
# KubernetesClusterIO with mocked CustomObjectsApi                            #
# --------------------------------------------------------------------------- #


def _api_exception(status: int) -> Exception:
    """Build a minimal kubernetes ApiException for test plumbing."""
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=status, reason="mocked")


def _make_cluster_with_mocked_api() -> tuple[KubernetesClusterIO, MagicMock]:
    cluster = KubernetesClusterIO()
    api = MagicMock()
    # Bypass the lazy loader — both clients must be set so _ensure_loaded()
    # short-circuits.
    cluster._custom_api = api
    cluster._core_api = MagicMock()
    return cluster, api


def _crd(name: str = "podchaos-x", ns: str = "default") -> dict[str, Any]:
    return {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "PodChaos",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"action": "pod-kill", "mode": "one"},
    }


async def test_apply_creates_when_not_exists() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    body = _crd()
    api.create_namespaced_custom_object.return_value = {**body, "metadata": {**body["metadata"], "uid": "u1"}}

    result = await cluster.apply(body)

    api.create_namespaced_custom_object.assert_called_once()
    kwargs = api.create_namespaced_custom_object.call_args.kwargs
    assert kwargs["group"] == "chaos-mesh.org"
    assert kwargs["version"] == "v1alpha1"
    assert kwargs["plural"] == "podchaos"
    assert kwargs["namespace"] == "default"
    assert result["metadata"]["uid"] == "u1"


async def test_apply_replaces_on_409_conflict() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    body = _crd()
    api.create_namespaced_custom_object.side_effect = _api_exception(409)
    api.get_namespaced_custom_object.return_value = {
        "metadata": {"name": "podchaos-x", "namespace": "default", "resourceVersion": "42"},
    }
    api.replace_namespaced_custom_object.return_value = {**body, "status": {"phase": "Running"}}

    result = await cluster.apply(body)

    api.replace_namespaced_custom_object.assert_called_once()
    replaced_body = api.replace_namespaced_custom_object.call_args.kwargs["body"]
    assert replaced_body["metadata"]["resourceVersion"] == "42"
    assert result["status"]["phase"] == "Running"


async def test_apply_propagates_non_409_errors() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    api.create_namespaced_custom_object.side_effect = _api_exception(500)
    from kubernetes.client.exceptions import ApiException

    with pytest.raises(ApiException):
        await cluster.apply(_crd())


async def test_get_returns_resource() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    api.get_namespaced_custom_object.return_value = _crd()

    r = await cluster.get("chaos-mesh.org/v1alpha1", "PodChaos", "podchaos-x", "default")
    assert r is not None
    assert r["kind"] == "PodChaos"


async def test_get_returns_none_on_404() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    api.get_namespaced_custom_object.side_effect = _api_exception(404)

    r = await cluster.get("chaos-mesh.org/v1alpha1", "PodChaos", "missing", "default")
    assert r is None


async def test_delete_returns_true_on_success() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    api.delete_namespaced_custom_object.return_value = {"status": "Success"}

    deleted = await cluster.delete(
        "chaos-mesh.org/v1alpha1", "PodChaos", "podchaos-x", "default"
    )
    assert deleted is True


async def test_delete_returns_false_on_404() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    api.delete_namespaced_custom_object.side_effect = _api_exception(404)

    deleted = await cluster.delete(
        "chaos-mesh.org/v1alpha1", "PodChaos", "missing", "default"
    )
    assert deleted is False


async def test_list_by_labels_builds_correct_selector() -> None:
    cluster, api = _make_cluster_with_mocked_api()
    api.list_namespaced_custom_object.return_value = {"items": [_crd()]}

    result = await cluster.list_by_labels(
        "chaos-mesh.org/v1alpha1",
        "PodChaos",
        "default",
        {"chaos.kosta.dev/experiment-id": "exp-abc", "chaos.kosta.dev/fault-name": "pod.kill"},
    )

    assert len(result) == 1
    kwargs = api.list_namespaced_custom_object.call_args.kwargs
    # Keys are sorted alphabetically; values are joined with ','.
    assert kwargs["label_selector"] == (
        "chaos.kosta.dev/experiment-id=exp-abc,"
        "chaos.kosta.dev/fault-name=pod.kill"
    )


async def test_list_by_labels_returns_empty_on_missing_crd() -> None:
    """If the resource kind isn't installed (404), return [] not raise — mirrors
    FakeClusterIO's behavior and matches what the chaos-agent cleanup() loop
    expects when sweeping kinds that may not exist."""
    cluster, api = _make_cluster_with_mocked_api()
    api.list_namespaced_custom_object.side_effect = _api_exception(404)

    result = await cluster.list_by_labels(
        "chaos-mesh.org/v1alpha1", "PodChaos", "default", {"x": "y"}
    )
    assert result == []
