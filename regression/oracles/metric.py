"""Metric oracle: a statistical-distribution resilience predicate.

Where the Playwright / command oracles are *boolean* (a journey passes or it
doesn't), this oracle is *distributional*: it samples a metric (latency, error
rate, throughput) into a ``StatisticalSample`` at baseline and again under fault,
then flags a regression when a percentile moves past its budget in the *worse*
direction. The same comparison drives two axes:

* **acute** — under-fault distribution vs the clean baseline (this run's verdict).
* **chronic** — the clean baseline vs a stored golden's distribution (drift). The
  baseline samples + thresholds are recorded in ``verify_result.evidence`` so
  ``goldens_from_run`` can freeze them and ``drift_report`` can compare later.

The single I/O boundary is ``_query_metric`` (a PromQL range query flattened to a
list of floats). Unit tests monkeypatch it with canned samples, so the parse +
comparison logic runs without a Prometheus.
"""

from __future__ import annotations

import time
from typing import Any

from plugins.base import ExperimentPlugin, PluginContext
from plugins.registry import register_plugin
from regression.drift import compare_metric
from shared.contracts import (
    FindingSeverity,
    MetricThreshold,
    StatisticalSample,
    VerifyFailure,
    VerifyResult,
)


def _thresholds(config: dict[str, Any]) -> list[MetricThreshold]:
    """Parse ``oracle_config['metrics']`` into thresholds. Each needs a query."""
    raw = config.get("metrics", [])
    if not isinstance(raw, list):
        raise ValueError("metric oracle: oracle_config['metrics'] must be a list")
    thresholds = [MetricThreshold.model_validate(entry) for entry in raw]
    missing = [t.metric for t in thresholds if not t.query]
    if missing:
        raise ValueError(
            f"metric oracle: metrics missing a PromQL 'query': {missing}"
        )
    return thresholds


@register_plugin
class MetricOraclePlugin(ExperimentPlugin):
    """Regression oracle backed by statistical metric distributions."""

    name = "regression-metric"

    async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
        samples = await self._sample_all(ctx)
        ctx.scratch["baseline_metrics"] = samples
        return samples

    async def verify(self, ctx: PluginContext) -> VerifyResult | None:
        thresholds = _thresholds(ctx.config)
        baseline: list[StatisticalSample] = ctx.scratch.get("baseline_metrics", [])
        baseline_by = {s.metric: s for s in baseline}
        # Serialized once — recorded on every branch so a golden (chronic drift)
        # can be frozen from this run and later diffed against.
        evidence_common: dict[str, Any] = {
            "baseline_metrics": [s.model_dump() for s in baseline],
            "metric_thresholds": [t.model_dump() for t in thresholds],
        }

        # No metric produced a baseline distribution — we can't assess resilience
        # (Prometheus down, or the queries matched nothing). BASELINE_FAIL, not a
        # misleading PASS over zero comparisons.
        if not baseline:
            return VerifyResult(
                passed=True,
                summary="no baseline metric samples (source empty); cannot assess",
                evidence={
                    **evidence_common,
                    "baseline_unassessable": True,
                    "newly_failing": [],
                },
            )

        current = await self._sample_all(ctx)
        current_by = {s.metric: s for s in current}

        regressions: list[VerifyFailure] = []
        for t in thresholds:
            g = baseline_by.get(t.metric)
            c = current_by.get(t.metric)
            if g is None or c is None:
                continue  # not sampled this run — nothing to compare, not a pass/fail
            drift = compare_metric(g, c, t)
            if drift.regressed:
                regressions.append(
                    VerifyFailure(
                        assertion=t.metric,
                        expected=f"{t.percentile} within {t.max_ratio:.2f}x of baseline "
                        f"{drift.golden_value:.3g}",
                        actual=f"{t.percentile}={drift.current_value:.3g} under fault",
                        severity=FindingSeverity.HIGH,
                        evidence={"ratio": drift.ratio, "direction": t.direction.value},
                    )
                )

        newly = [f.assertion for f in regressions]
        if not regressions:
            return VerifyResult(
                passed=True,
                summary="all metric distributions held within budget under fault",
                evidence={**evidence_common, "newly_failing": []},
            )
        return VerifyResult(
            passed=False,
            summary=f"{len(regressions)} metric(s) regressed under fault",
            failures=regressions,
            evidence={**evidence_common, "newly_failing": newly},
        )

    # ----- I/O boundary (monkeypatched in tests) --------------------------
    async def _sample_all(self, ctx: PluginContext) -> list[StatisticalSample]:
        """Sample every configured metric into a distribution. Empty queries are
        dropped (recorded nowhere) so an unassessable run surfaces cleanly."""
        out: list[StatisticalSample] = []
        for t in _thresholds(ctx.config):
            values = await self._query_metric(ctx.config, t.query)
            if values:
                out.append(StatisticalSample.from_samples(t.metric, values))
        return out

    async def _query_metric(
        self, config: dict[str, Any], query: str
    ) -> list[float]:
        # Dry run: a canned flat distribution so `regression run --dry-run`
        # exercises the flow without Prometheus (never regresses vs itself).
        if config.get("_dry_run"):
            return [100.0, 100.0, 100.0]

        from agents.tester.tools.prometheus import HttpxPromBackend

        prom_url = config.get("prom_url")
        backend = (
            HttpxPromBackend(str(prom_url))
            if prom_url
            else HttpxPromBackend.from_env()
        )
        window = float(config.get("window_seconds", 300.0))
        step = float(config.get("step_seconds", 15.0))
        end = time.time()
        series = await backend.query_range(
            query, start=end - window, end=end, step_seconds=step
        )
        # Flatten every series' points into one distribution; NaNs dropped.
        return [
            pt.value
            for one in series
            for pt in one
            if pt.value == pt.value  # NaN != NaN
        ]
