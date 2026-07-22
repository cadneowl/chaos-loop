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
    RegressionVerdict,
    ScenarioDrift,
    SuiteRunRecord,
)


def baseline_passing(verdict: RegressionVerdict) -> list[str]:
    """The journeys that passed at baseline in this scenario's run, if recorded."""
    vr = verdict.verify_result
    if vr is None:
        return []
    return [str(x) for x in vr.evidence.get("baseline_passing", [])]


def goldens_from_run(record: SuiteRunRecord, target_ref: str) -> dict[str, Golden]:
    """Build a golden per scenario from a completed suite run (scenario_id -> Golden)."""
    return {
        verdict.scenario_id: Golden(
            target_ref=target_ref, passing_journeys=baseline_passing(verdict)
        )
        for verdict in record.verdicts
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
    scenarios = [
        compute_scenario_drift(
            goldens.get(verdict.scenario_id),
            baseline_passing(verdict),
            scenario_id=verdict.scenario_id,
            title=verdict.title,
        )
        for verdict in record.verdicts
    ]
    return DriftReport(
        suite_id=record.suite_id, against_ref=against_ref, scenarios=scenarios
    )
