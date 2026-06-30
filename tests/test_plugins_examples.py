"""Branch coverage for the example plugins + their fakes + base defaults."""

from __future__ import annotations

import pytest

from plugins.base import ExperimentPlugin, PluginContext
from plugins.examples._fakes import FakeCluster, FakeResponse, FakeService
from plugins.examples.keyvalue_scenario import KeyValueScenario
from plugins.examples.web_service_scenario import WebServiceScenario
from plugins.host import GuardTripped, open_session
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    SafetyConstraints,
)


def _plan(plugin: str, **cfg: object) -> ExperimentPlan:
    return ExperimentPlan(
        title="t",
        target_app="app",
        plugin=plugin,
        plugin_config=dict(cfg),
        faults=[
            FaultSpec(
                category=FaultCategory.NETWORK,
                name="network.loss",
                target_selector={"app": "app"},
                duration_seconds=1,
                rationale="r",
            )
        ],
        safety=SafetyConstraints(
            cluster_context="kind-dev", namespace="demo", require_namespace_annotation=False
        ),
    )


async def _slow_inject() -> str:
    import asyncio

    await asyncio.sleep(0.05)
    return "done"  # pragma: no cover


# --- KeyValueScenario --------------------------------------------------------


async def test_kv_validate_rejects_bad_guard_min() -> None:
    with pytest.raises(ValueError, match="guard_min_keys"):
        async with open_session(_plan("example-keyvalue", guard_min_keys=-1), KeyValueScenario()):
            pass  # pragma: no cover


async def test_kv_guard_trips_on_data_loss() -> None:
    plan = _plan(
        "example-keyvalue", seed_keys=["a", "b", "c"], inject_data_loss=True, guard_min_keys=3
    )
    async with open_session(plan, KeyValueScenario()) as session:
        with pytest.raises(GuardTripped):
            await session.drive_run(_slow_inject)


async def test_kv_guard_disabled_when_zero() -> None:
    plan = _plan("example-keyvalue", guard_min_keys=0)
    async with open_session(plan, KeyValueScenario()) as session:
        # No guard, no run_test override path differences: verify still passes.
        vr = await session.verify()
    assert vr is not None and vr.passed


# --- WebServiceScenario validation ------------------------------------------


async def test_web_validate_rejects_bad_error_rate() -> None:
    with pytest.raises(ValueError, match="slo_error_rate"):
        async with open_session(_plan("example-web-service", slo_error_rate=2.0), WebServiceScenario()):
            pass  # pragma: no cover


async def test_web_validate_rejects_zero_replicas() -> None:
    with pytest.raises(ValueError, match="replicas"):
        async with open_session(_plan("example-web-service", replicas=0), WebServiceScenario()):
            pass  # pragma: no cover


# --- _fakes ------------------------------------------------------------------


def test_fake_response_ok() -> None:
    assert FakeResponse(200, 10).ok
    assert not FakeResponse(503, 10).ok


def test_fake_service_seed_and_degrade() -> None:
    svc = FakeService()
    svc.put("k", "v")
    assert svc.get("k") == "v"
    svc.drop("k")
    assert svc.get("k") is None

    # Healthy requests are 200.
    assert all(svc.request().status == 200 for _ in range(5))
    # Degraded requests include 503s, then recover.
    svc.degrade()
    statuses = [svc.request().status for _ in range(6)]
    assert 503 in statuses
    svc.recover()
    assert svc.request().status == 200


async def test_fake_cluster_lifecycle() -> None:
    cluster = FakeCluster(ready_after=1)
    await cluster.apply("svc", image="img:1", replicas=2)
    assert cluster.names() == ["svc"]
    await cluster.wait_ready("svc", max_polls=5)
    assert await cluster.delete("svc") is True
    assert await cluster.delete("svc") is False  # already gone
    assert cluster.names() == []


# --- base defaults -----------------------------------------------------------


async def test_base_plugin_hooks_are_noops() -> None:
    p = ExperimentPlugin()
    ctx = PluginContext(experiment_id="exp-000000000abc", plan=_plan("base"))
    assert await p.validate(ctx) is None
    assert await p.provision_env(ctx) is None
    assert await p.await_ready(ctx) is None
    assert await p.seed(ctx) is None
    assert await p.setup_test(ctx) is None
    assert await p.capture_baseline(ctx) == []
    assert await p.run_test(ctx) is None
    assert p.steady_state_guard(ctx) is None
    assert await p.verify(ctx) is None
    assert await p.collect_diagnostics(ctx) == {}
    assert await p.teardown_test(ctx) is None
    assert await p.teardown_env(ctx) is None
