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
    RegressionOutcome,
    RegressionVerdict,
    ScenarioDrift,
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


def goldens_from_run(record: SuiteRunRecord, target_ref: str) -> dict[str, Golden]:
    """Build a golden per scenario from a completed suite run (scenario_id -> Golden).

    Scenarios whose baseline didn't measure cleanly (ERROR / BASELINE_FAIL) are
    skipped rather than frozen as an empty/partial golden — a poisoned golden
    would silently blind drift detection for that scenario on every later run.
    """
    return {
        verdict.scenario_id: Golden(
            target_ref=target_ref, passing_journeys=baseline_passing(verdict)
        )
        for verdict in record.verdicts
        if baseline_trustworthy(verdict)
    }


def compute_scenario_drift(
    golden: Golden | None,
    current_passing: list[str],
    *,
    scenario_id: str,
    title: str = "",
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
            )
        )
    return DriftReport(
        suite_id=record.suite_id, against_ref=against_ref, scenarios=scenarios
    )
