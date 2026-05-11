"""Tests for ClaudeChaosAgent.execute() against FakeClusterIO."""

from __future__ import annotations

import asyncio

import pytest

from agents.chaos.agent import ClaudeChaosAgent
from agents.chaos.cluster import FakeClusterIO
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    SafetyConstraints,
)

# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


async def _no_sleep(_: float) -> None:
    return None


def _plan(
    *,
    fault_name: str = "network.loss",
    fault_category: FaultCategory = FaultCategory.NETWORK,
    faults: list[FaultSpec] | None = None,
    allow_multi: bool = False,
    quiet_pre: int = 0,
    quiet_post: int = 0,
    experiment_id: str = "exp-000000000abc",
    parameters: dict | None = None,
) -> ExperimentPlan:
    if faults is None:
        faults = [
            FaultSpec(
                category=fault_category,
                name=fault_name,
                target_selector={"app": "cart"},
                parameters=parameters or {},
                duration_seconds=10,
                rationale="test",
            )
        ]
    return ExperimentPlan(
        experiment_id=experiment_id,
        title="test",
        target_app="otel-demo",
        faults=faults,
        safety=SafetyConstraints(
            cluster_context="kind-chaos",
            namespace="otel-demo",
            allow_multi_fault=allow_multi,
        ),
        quiet_window_pre_seconds=quiet_pre,
        quiet_window_post_seconds=quiet_post,
    )


def _agent_with(cluster: FakeClusterIO | None = None) -> ClaudeChaosAgent:
    return ClaudeChaosAgent(cluster=cluster, sleep_fn=_no_sleep)


# ---------------------------------------------------------------------------- #
# Pre-flight                                                                   #
# ---------------------------------------------------------------------------- #


def test_execute_fails_without_cluster() -> None:
    agent = ClaudeChaosAgent(cluster=None, sleep_fn=_no_sleep)
    timeline = asyncio.run(agent.execute(_plan()))
    assert not timeline.success
    assert "cluster" in (timeline.error or "")


def test_execute_rejects_unknown_fault() -> None:
    bad_fault = FaultSpec(
        category=FaultCategory.NETWORK,
        name="not.a.real.fault",
        target_selector={"a": "b"},
        duration_seconds=1,
        rationale="r",
    )
    plan = _plan(faults=[bad_fault])
    cluster = FakeClusterIO()
    timeline = asyncio.run(_agent_with(cluster).execute(plan))
    assert not timeline.success
    assert "not in catalogue" in (timeline.error or "")
    # Should not have applied anything.
    assert cluster.applied == []


def test_execute_rejects_unrendered_fault() -> None:
    """An unrendered-but-catalogued fault (e.g., a security one in M3.0a) -> error."""
    plan = _plan(fault_name="cert.revoke", fault_category=FaultCategory.CERT)
    cluster = FakeClusterIO()
    timeline = asyncio.run(_agent_with(cluster).execute(plan))
    assert not timeline.success
    assert "no renderer" in (timeline.error or "")
    assert cluster.applied == []


def test_execute_rejects_multi_fault_without_flag() -> None:
    faults = [
        FaultSpec(
            category=FaultCategory.NETWORK,
            name="network.loss",
            target_selector={"a": "b"},
            duration_seconds=5,
            rationale="r",
        ),
        FaultSpec(
            category=FaultCategory.POD,
            name="pod.kill",
            target_selector={"a": "b"},
            duration_seconds=5,
            rationale="r",
        ),
    ]
    plan = _plan(faults=faults, allow_multi=False)
    cluster = FakeClusterIO()
    timeline = asyncio.run(_agent_with(cluster).execute(plan))
    assert not timeline.success
    assert "multi_fault" in (timeline.error or "") or "multiple faults" in (timeline.error or "")


# ---------------------------------------------------------------------------- #
# Happy path                                                                   #
# ---------------------------------------------------------------------------- #


def test_execute_happy_path_single_fault() -> None:
    cluster = FakeClusterIO()
    timeline = asyncio.run(_agent_with(cluster).execute(_plan()))
    assert timeline.success, timeline.error
    # Exactly one apply.
    assert len(cluster.applied) == 1
    body = cluster.applied[0]
    assert body["kind"] == "NetworkChaos"
    assert body["metadata"]["labels"]["chaos.kosta.dev/experiment-id"] == "exp-000000000abc"
    # Resource was deleted after duration.
    assert len(cluster.deleted) == 1
    assert cluster.deleted[0][1] == "NetworkChaos"
    # Timeline events: scheduled, started, cleaned-up.
    event_types = [e.event for e in timeline.events]
    assert event_types == ["scheduled", "started", "cleaned-up"]


def test_execute_quiet_windows_honored() -> None:
    """We can't observe sleep duration in a fake-sleep run, but the call sequence
    is what we test (no extra applies during quiet windows)."""
    cluster = FakeClusterIO()
    timeline = asyncio.run(_agent_with(cluster).execute(_plan(quiet_pre=30, quiet_post=30)))
    assert timeline.success
    assert len(cluster.applied) == 1


def test_execute_multi_fault_with_flag_runs_in_sequence() -> None:
    faults = [
        FaultSpec(
            category=FaultCategory.NETWORK,
            name="network.loss",
            target_selector={"a": "b"},
            duration_seconds=5,
            rationale="r",
        ),
        FaultSpec(
            category=FaultCategory.POD,
            name="pod.kill",
            target_selector={"a": "b"},
            duration_seconds=5,
            rationale="r",
        ),
    ]
    plan = _plan(faults=faults, allow_multi=True)
    cluster = FakeClusterIO()
    timeline = asyncio.run(_agent_with(cluster).execute(plan))
    assert timeline.success, timeline.error
    assert len(cluster.applied) == 2
    # First applied is NetworkChaos, second is PodChaos.
    assert cluster.applied[0]["kind"] == "NetworkChaos"
    assert cluster.applied[1]["kind"] == "PodChaos"


# ---------------------------------------------------------------------------- #
# Failure paths                                                                #
# ---------------------------------------------------------------------------- #


def test_execute_recovers_from_apply_failure_with_cleanup() -> None:
    """If apply raises, cleanup runs and timeline reports failure."""
    cluster = FakeClusterIO()

    async def boom(_body: dict) -> None:
        raise RuntimeError("simulated apply failure")

    cluster.apply_hook = boom
    timeline = asyncio.run(_agent_with(cluster).execute(_plan()))
    assert not timeline.success
    assert "simulated apply failure" in (timeline.error or "")
    # cleanup() was called and listed by labels; with nothing applied there's
    # nothing to delete, but the attempt happened — verified by the timeline
    # carrying an explicit error event.
    assert any(e.event == "error" for e in timeline.events)


def test_cleanup_deletes_resources_by_label() -> None:
    """Directly test cleanup() against pre-populated resources."""
    cluster = FakeClusterIO()
    # Pre-populate as if a prior run left orphans.
    body = {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "NetworkChaos",
        "metadata": {
            "name": "orphan-1",
            "namespace": "otel-demo",
            "labels": {"chaos.kosta.dev/experiment-id": "exp-000000000abc"},
        },
        "spec": {},
    }
    cluster.resources[("chaos-mesh.org/v1alpha1", "NetworkChaos", "otel-demo", "orphan-1")] = body

    agent = _agent_with(cluster)
    asyncio.run(agent.cleanup(_plan()))
    # The orphan with the matching experiment-id label should have been deleted.
    assert ("chaos-mesh.org/v1alpha1", "NetworkChaos", "otel-demo", "orphan-1") in cluster.deleted


def test_cleanup_skips_resources_with_other_experiment_label() -> None:
    cluster = FakeClusterIO()
    body = {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "NetworkChaos",
        "metadata": {
            "name": "someone-elses",
            "namespace": "otel-demo",
            "labels": {"chaos.kosta.dev/experiment-id": "exp-different00000"},
        },
        "spec": {},
    }
    cluster.resources[
        ("chaos-mesh.org/v1alpha1", "NetworkChaos", "otel-demo", "someone-elses")
    ] = body

    agent = _agent_with(cluster)
    asyncio.run(agent.cleanup(_plan()))
    # The other experiment's resource should NOT be deleted.
    assert cluster.deleted == []


# ---------------------------------------------------------------------------- #
# FakeClusterIO direct sanity                                                  #
# ---------------------------------------------------------------------------- #


def test_fake_cluster_io_roundtrip() -> None:
    cluster = FakeClusterIO()
    body = {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": "NetworkChaos",
        "metadata": {"name": "x", "namespace": "ns", "labels": {"app": "a"}},
        "spec": {},
    }
    asyncio.run(cluster.apply(body))
    got = asyncio.run(cluster.get("chaos-mesh.org/v1alpha1", "NetworkChaos", "x", "ns"))
    assert got is not None
    assert got["metadata"]["name"] == "x"
    # Server-set status appended.
    assert got["status"]["phase"] == "Running"

    matched = asyncio.run(
        cluster.list_by_labels("chaos-mesh.org/v1alpha1", "NetworkChaos", "ns", {"app": "a"})
    )
    assert len(matched) == 1

    existed = asyncio.run(cluster.delete("chaos-mesh.org/v1alpha1", "NetworkChaos", "x", "ns"))
    assert existed
    again = asyncio.run(cluster.delete("chaos-mesh.org/v1alpha1", "NetworkChaos", "x", "ns"))
    assert not again  # idempotent


@pytest.mark.parametrize(
    "fault_name,expected_kind",
    [
        ("pod.kill", "PodChaos"),
        ("network.delay", "NetworkChaos"),
        ("stress.cpu", "StressChaos"),
    ],
)
def test_execute_dispatches_to_correct_kind(fault_name: str, expected_kind: str) -> None:
    cat_map = {
        "pod.kill": FaultCategory.POD,
        "network.delay": FaultCategory.NETWORK,
        "stress.cpu": FaultCategory.STRESS,
    }
    cluster = FakeClusterIO()
    timeline = asyncio.run(
        _agent_with(cluster).execute(_plan(fault_name=fault_name, fault_category=cat_map[fault_name]))
    )
    assert timeline.success
    assert cluster.applied[0]["kind"] == expected_kind
