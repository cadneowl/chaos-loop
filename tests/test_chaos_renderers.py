"""Tests for chaos agent CRD renderers. Pure-function tests, no cluster."""

from __future__ import annotations

import pytest

from agents.chaos.faults._render import RenderContext
from agents.chaos.faults.registry import RENDERERS, render, unrendered_faults
from shared.contracts import FaultCategory, FaultSpec

CTX = RenderContext(namespace="otel-demo", experiment_id="exp-aaaaaaaaaaaa")


def _spec(name: str, category: FaultCategory, **params) -> FaultSpec:
    return FaultSpec(
        category=category,
        name=name,
        target_selector={"app.kubernetes.io/component": "cartservice"},
        parameters=params,
        duration_seconds=60,
        rationale="test",
    )


# ---------------------------------------------------------------------------- #
# Registry                                                                     #
# ---------------------------------------------------------------------------- #


def test_registry_covers_basic_classical_faults() -> None:
    """M3.0a milestone: at least these renderers exist."""
    expected = {
        "pod.kill",
        "pod.failure",
        "network.loss",
        "network.delay",
        "network.partition",
        "stress.cpu",
        "stress.memory",
    }
    assert expected <= set(RENDERERS)


def test_render_rejects_unknown_fault() -> None:
    spec = FaultSpec(
        category=FaultCategory.POD,
        name="totally.made.up",
        target_selector={"a": "b"},
        duration_seconds=10,
        rationale="r",
    )
    with pytest.raises(KeyError, match="not in catalogue"):
        render(spec, CTX)


def test_render_reports_unrendered_faults() -> None:
    """Sanity check: the catalogue has more faults than M3.0a renders.

    These will gradually move into RENDERERS as later milestones land.
    """
    todo = unrendered_faults()
    # The security-flavored faults should still be unrendered at M3.0a.
    assert "cert.revoke" in todo
    assert "image.swap_vuln" in todo


# ---------------------------------------------------------------------------- #
# Pod renderers                                                                #
# ---------------------------------------------------------------------------- #


def test_pod_kill_render() -> None:
    spec = _spec("pod.kill", FaultCategory.POD)
    crd = render(spec, CTX)

    assert crd["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert crd["kind"] == "PodChaos"
    assert crd["spec"]["action"] == "pod-kill"
    assert crd["spec"]["mode"] == "one"
    assert crd["spec"]["selector"]["namespaces"] == ["otel-demo"]
    assert crd["spec"]["selector"]["labelSelectors"] == {
        "app.kubernetes.io/component": "cartservice"
    }
    # No `duration` for pod-kill (it's a one-shot action).
    assert "duration" not in crd["spec"]


def test_pod_failure_includes_duration() -> None:
    spec = _spec("pod.failure", FaultCategory.POD)
    crd = render(spec, CTX)
    assert crd["spec"]["action"] == "pod-failure"
    assert crd["spec"]["duration"] == "60s"


def test_pod_kill_grace_period_passes_through() -> None:
    spec = _spec("pod.kill", FaultCategory.POD, grace_period_seconds=30)
    crd = render(spec, CTX)
    assert crd["spec"]["gracePeriod"] == 30


# ---------------------------------------------------------------------------- #
# Network renderers                                                            #
# ---------------------------------------------------------------------------- #


def test_network_loss_defaults() -> None:
    spec = _spec("network.loss", FaultCategory.NETWORK)
    crd = render(spec, CTX)
    assert crd["kind"] == "NetworkChaos"
    assert crd["spec"]["action"] == "loss"
    assert crd["spec"]["direction"] == "to"
    assert crd["spec"]["loss"] == {"loss": "100", "correlation": "0"}
    assert crd["spec"]["duration"] == "60s"


def test_network_loss_custom_percent() -> None:
    spec = _spec("network.loss", FaultCategory.NETWORK, loss_percent=37)
    crd = render(spec, CTX)
    assert crd["spec"]["loss"]["loss"] == "37"


def test_network_delay_emits_ms_strings() -> None:
    spec = _spec("network.delay", FaultCategory.NETWORK, latency_ms=250, jitter_ms=50)
    crd = render(spec, CTX)
    assert crd["spec"]["action"] == "delay"
    assert crd["spec"]["delay"]["latency"] == "250ms"
    assert crd["spec"]["delay"]["jitter"] == "50ms"


def test_network_partition_symmetric() -> None:
    spec = _spec(
        "network.partition",
        FaultCategory.NETWORK,
        target_selector_other={"app.kubernetes.io/component": "checkoutservice"},
    )
    crd = render(spec, CTX)
    assert crd["spec"]["action"] == "partition"
    assert crd["spec"]["direction"] == "both"
    assert crd["spec"]["target"]["selector"]["labelSelectors"] == {
        "app.kubernetes.io/component": "checkoutservice"
    }


# ---------------------------------------------------------------------------- #
# Stress renderers                                                             #
# ---------------------------------------------------------------------------- #


def test_stress_cpu_defaults() -> None:
    spec = _spec("stress.cpu", FaultCategory.STRESS)
    crd = render(spec, CTX)
    assert crd["kind"] == "StressChaos"
    assert crd["spec"]["stressors"]["cpu"] == {"workers": 1, "load": 100}


def test_stress_memory_custom_size() -> None:
    spec = _spec("stress.memory", FaultCategory.STRESS, workers=2, size="512MB")
    crd = render(spec, CTX)
    assert crd["spec"]["stressors"]["memory"] == {"workers": 2, "size": "512MB"}


# ---------------------------------------------------------------------------- #
# Metadata invariants                                                          #
# ---------------------------------------------------------------------------- #


def test_metadata_labels_set_on_every_renderer() -> None:
    """Every renderer must label its CRD so cleanup can find it."""
    for name in RENDERERS:
        spec = _spec(
            name,
            # category here is only used to construct the FaultSpec; not checked
            FaultCategory.POD,
            **(
                {"target_selector_other": {"app": "x"}} if name == "network.partition" else {}
            ),
        )
        crd = render(spec, CTX)
        labels = crd["metadata"]["labels"]
        assert labels["chaos.kosta.dev/experiment-id"] == "exp-aaaaaaaaaaaa"
        assert labels["chaos.kosta.dev/fault-name"] == name
        assert crd["metadata"]["namespace"] == "otel-demo"


def test_resource_name_is_stable_and_sub_63_chars() -> None:
    spec = _spec("network.partition", FaultCategory.NETWORK, target_selector_other={"a": "b"})
    crd = render(spec, CTX)
    name = crd["metadata"]["name"]
    assert len(name) <= 63
    # Same input -> same output.
    crd2 = render(spec, CTX)
    assert crd2["metadata"]["name"] == name
