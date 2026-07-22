"""Metric oracle: distribution sampling and the under-fault regression verdict.

The Prometheus boundary (``_query_metric``) is monkeypatched with canned float
lists, so these exercise the StatisticalSample build + threshold comparison
without a Prometheus.
"""

from __future__ import annotations

from typing import Any

from plugins.base import PluginContext
from regression.oracles.metric import MetricOraclePlugin, _thresholds
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


def _stub_query(plugin: MetricOraclePlugin, by_query: dict[str, list[list[float]]]) -> None:
    """Serve canned samples per PromQL query; pops one list per call (baseline, then fault)."""
    queues = {q: list(runs) for q, runs in by_query.items()}

    async def fake(_config: dict[str, Any], query: str) -> list[float]:
        return queues[query].pop(0)

    plugin._query_metric = fake  # type: ignore[method-assign]


_LATENCY = {
    "metrics": [
        {"metric": "checkout_latency", "query": "histogram_quantile(...)", "percentile": "p95"}
    ]
}


def test_thresholds_require_a_query() -> None:
    import pytest

    with pytest.raises(ValueError, match="missing a PromQL 'query'"):
        _thresholds({"metrics": [{"metric": "x"}]})


async def test_latency_within_budget_passes() -> None:
    plugin = MetricOraclePlugin()
    _stub_query(
        plugin,
        {"histogram_quantile(...)": [[100.0, 100.0, 100.0], [105.0, 105.0, 105.0]]},
    )
    ctx = _ctx(dict(_LATENCY))
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is True
    assert result.evidence["newly_failing"] == []
    # baseline distribution + thresholds recorded for golden capture (chronic drift).
    assert result.evidence["baseline_metrics"][0]["metric"] == "checkout_latency"
    assert result.evidence["metric_thresholds"][0]["percentile"] == "p95"


async def test_latency_blowout_regresses() -> None:
    plugin = MetricOraclePlugin()
    # p95 jumps 100 -> 200 under fault, well past the default 1.10x budget.
    _stub_query(
        plugin,
        {"histogram_quantile(...)": [[100.0, 100.0, 100.0], [200.0, 200.0, 200.0]]},
    )
    ctx = _ctx(dict(_LATENCY))
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is False
    assert result.evidence["newly_failing"] == ["checkout_latency"]
    assert result.failures[0].assertion == "checkout_latency"


async def test_empty_baseline_is_unassessable() -> None:
    plugin = MetricOraclePlugin()
    _stub_query(plugin, {"histogram_quantile(...)": [[]]})  # source returns nothing
    ctx = _ctx(dict(_LATENCY))
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.evidence["baseline_unassessable"] is True


async def test_lower_worse_direction_flags_a_throughput_drop() -> None:
    plugin = MetricOraclePlugin()
    cfg = {
        "metrics": [
            {
                "metric": "rps",
                "query": "sum(rate(...))",
                "percentile": "p50",
                "direction": "lower_worse",
            }
        ]
    }
    # Throughput halves under fault (1000 -> 400); lower is worse -> regression.
    _stub_query(plugin, {"sum(rate(...))": [[1000.0, 1000.0], [400.0, 400.0]]})
    ctx = _ctx(cfg)
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is False
    assert result.evidence["newly_failing"] == ["rps"]


async def test_dry_run_passes_without_prometheus() -> None:
    plugin = MetricOraclePlugin()
    ctx = _ctx({**_LATENCY, "_dry_run": True})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is True
