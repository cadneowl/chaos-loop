"""Chronic drift axis: compare a fresh baseline to a stored golden.

The oracle records, on every run, which journeys passed at baseline
(``verify_result.evidence["baseline_passing"]``). Saving those as a ``Golden``
pins the steady state for a target ref; a later run's baseline is diffed against
it to surface **drift** — journeys that quietly went red at baseline between
releases, which the acute (under-fault) delta can't see.

All functions here are pure; the run itself and persistence live elsewhere.
"""

from __future__ import annotations

from shared.contracts import (
    DriftReport,
    Golden,
    MetricDirection,
    MetricDrift,
    MetricThreshold,
    RegressionOutcome,
    RegressionVerdict,
    ScenarioDrift,
    StatisticalSample,
    SuiteRunRecord,
)

# A scenario's baseline is trustworthy only when it was actually measured: PASS
# (green at baseline, survived the fault) or REGRESSED (green at baseline, broke
# under fault — the baseline itself is still valid). ERROR / BASELINE_FAIL mean the
# baseline never measured cleanly, so ``baseline_passing`` would be empty or partial
# — freezing that as a golden (or diffing against it) yields phantom drift.
_TRUSTWORTHY_OUTCOMES = frozenset({RegressionOutcome.PASS, RegressionOutcome.REGRESSED})


def baseline_trustworthy(verdict: RegressionVerdict) -> bool:
    """Whether this scenario's baseline measured cleanly enough to use for drift."""
    return verdict.outcome in _TRUSTWORTHY_OUTCOMES


def baseline_passing(verdict: RegressionVerdict) -> list[str]:
    """The journeys that passed at baseline in this scenario's run, if recorded."""
    vr = verdict.verify_result
    if vr is None:
        return []
    return [str(x) for x in vr.evidence.get("baseline_passing", [])]


def baseline_metrics(verdict: RegressionVerdict) -> list[StatisticalSample]:
    """The baseline metric distributions recorded by a metric oracle, if any."""
    vr = verdict.verify_result
    if vr is None:
        return []
    return [
        StatisticalSample.model_validate(s)
        for s in vr.evidence.get("baseline_metrics", [])
    ]


def metric_thresholds(verdict: RegressionVerdict) -> list[MetricThreshold]:
    """The metric drift budgets the oracle applied this run, if any."""
    vr = verdict.verify_result
    if vr is None:
        return []
    return [
        MetricThreshold.model_validate(t)
        for t in vr.evidence.get("metric_thresholds", [])
    ]


def goldens_from_run(record: SuiteRunRecord, target_ref: str) -> dict[str, Golden]:
    """Build a golden per scenario from a completed suite run (scenario_id -> Golden).

    Freezes both axes' baselines: the passing-journey set (boolean) and the
    metric distributions (statistical). Scenarios whose baseline didn't measure
    cleanly (ERROR / BASELINE_FAIL) are skipped rather than frozen as an
    empty/partial golden — a poisoned golden would silently blind drift detection
    for that scenario on every later run.
    """
    return {
        verdict.scenario_id: Golden(
            target_ref=target_ref,
            passing_journeys=baseline_passing(verdict),
            metrics=baseline_metrics(verdict),
        )
        for verdict in record.verdicts
        if baseline_trustworthy(verdict)
    }


# ----- metric-distribution drift --------------------------------------------


def sample_value(sample: StatisticalSample, percentile: str) -> float:
    """Read one summary statistic (mean / p50 / p95 / p99) off a distribution."""
    return float(getattr(sample, percentile))


def compare_metric(
    golden: StatisticalSample, current: StatisticalSample, threshold: MetricThreshold
) -> MetricDrift:
    """Compare one metric's percentile against its golden, per the threshold.

    A regression is a move past ``golden * max_ratio`` in the *worse* direction.
    Baselines at or below ``abs_floor`` are never flagged — a near-zero golden
    makes any absolute move a huge ratio, which would flap.
    """
    gv = sample_value(golden, threshold.percentile)
    cv = sample_value(current, threshold.percentile)
    if gv <= threshold.abs_floor:
        regressed = False
    elif threshold.direction == MetricDirection.HIGHER_WORSE:
        regressed = cv > gv * threshold.max_ratio
    else:  # LOWER_WORSE
        regressed = cv < gv / threshold.max_ratio
    return MetricDrift(
        metric=threshold.metric,
        percentile=threshold.percentile,
        direction=threshold.direction,
        golden_value=gv,
        current_value=cv,
        max_ratio=threshold.max_ratio,
        regressed=regressed,
    )


def compare_metrics(
    golden_metrics: list[StatisticalSample],
    current_metrics: list[StatisticalSample],
    thresholds: list[MetricThreshold],
) -> list[MetricDrift]:
    """One ``MetricDrift`` per threshold. Thresholds define what's asserted; a
    metric present in the golden with no threshold is not checked. A threshold
    whose metric is absent from either side is reported ``missing`` (never a
    silent pass)."""
    golden_by = {s.metric: s for s in golden_metrics}
    current_by = {s.metric: s for s in current_metrics}
    drifts: list[MetricDrift] = []
    for t in thresholds:
        g = golden_by.get(t.metric)
        c = current_by.get(t.metric)
        if g is None or c is None:
            drifts.append(
                MetricDrift(
                    metric=t.metric,
                    percentile=t.percentile,
                    direction=t.direction,
                    golden_value=sample_value(g, t.percentile) if g else 0.0,
                    current_value=sample_value(c, t.percentile) if c else 0.0,
                    max_ratio=t.max_ratio,
                    regressed=False,
                    missing=True,
                )
            )
        else:
            drifts.append(compare_metric(g, c, t))
    return drifts


def compute_scenario_drift(
    golden: Golden | None,
    current_passing: list[str],
    *,
    scenario_id: str,
    title: str = "",
    current_metrics: list[StatisticalSample] | None = None,
    thresholds: list[MetricThreshold] | None = None,
) -> ScenarioDrift:
    if golden is None:
        return ScenarioDrift(scenario_id=scenario_id, title=title, missing_golden=True)
    was = set(golden.passing_journeys)
    now = set(current_passing)
    return ScenarioDrift(
        scenario_id=scenario_id,
        title=title,
        regressed=sorted(was - now),  # green then, red now
        recovered=sorted(now - was),  # not green then, green now
        metric_drifts=compare_metrics(
            golden.metrics, current_metrics or [], thresholds or []
        ),
    )


def drift_report(
    record: SuiteRunRecord, goldens: dict[str, Golden], against_ref: str
) -> DriftReport:
    scenarios: list[ScenarioDrift] = []
    for verdict in record.verdicts:
        # A scenario whose baseline didn't measure cleanly this run can't be
        # compared — its baseline_passing is empty/partial and would report every
        # golden journey as falsely regressed. Flag it instead of inventing drift.
        if not baseline_trustworthy(verdict):
            scenarios.append(
                ScenarioDrift(
                    scenario_id=verdict.scenario_id,
                    title=verdict.title,
                    unassessed=True,
                )
            )
            continue
        scenarios.append(
            compute_scenario_drift(
                goldens.get(verdict.scenario_id),
                baseline_passing(verdict),
                scenario_id=verdict.scenario_id,
                title=verdict.title,
                current_metrics=baseline_metrics(verdict),
                thresholds=metric_thresholds(verdict),
            )
        )
    return DriftReport(
        suite_id=record.suite_id, against_ref=against_ref, scenarios=scenarios
    )
