"""Command oracle: exit-code delta (baseline-green -> fault-red = regression).

``_exec`` (the subprocess boundary) is monkeypatched with canned exit codes.
"""

from __future__ import annotations

from typing import Any

from plugins.base import PluginContext
from regression.oracles.command import CommandOraclePlugin
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    SafetyConstraints,
)


def _ctx(config: dict[str, Any]) -> PluginContext:
    plan = ExperimentPlan(
        title="t",
        target_app="t",
        faults=[
            FaultSpec(
                category=FaultCategory.POD,
                name="pod.kill",
                target_selector={"app": "x"},
                duration_seconds=1,
                rationale="r",
            )
        ],
        safety=SafetyConstraints(cluster_context="kind-test", namespace="default"),
    )
    return PluginContext(experiment_id=plan.experiment_id, plan=plan, config=config)


def _stub_exec(plugin: CommandOraclePlugin, codes: list[int]) -> None:
    queue = list(codes)

    async def fake(_config: dict[str, Any]) -> int:
        return queue.pop(0)

    plugin._exec = fake  # type: ignore[method-assign]


async def test_green_then_red_is_regression() -> None:
    plugin = CommandOraclePlugin()
    _stub_exec(plugin, [0, 1])  # baseline ok, fault fails
    ctx = _ctx({"name": "pytest"})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is False
    assert result.evidence["newly_failing"] == ["pytest"]


async def test_survives_fault_is_pass() -> None:
    plugin = CommandOraclePlugin()
    _stub_exec(plugin, [0, 0])  # ok before and under fault
    ctx = _ctx({"name": "pytest"})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is True


async def test_already_failing_at_baseline_is_not_a_regression() -> None:
    plugin = CommandOraclePlugin()
    _stub_exec(plugin, [1, 1])  # broken before the fault too
    ctx = _ctx({"name": "pytest"})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is True
    assert result.evidence["newly_failing"] == []
