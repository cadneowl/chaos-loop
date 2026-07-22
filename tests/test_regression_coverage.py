"""CoverageReporter: covered vs unknown cells, rollups, and the fault filter.

v1 never emits NA — a cell is COVERED (a scenario pairs the fault with the
journey) or UNKNOWN (treated as a gap). Evidence-backed NA is a v2 feature.
"""

from __future__ import annotations

from regression.coverage import CoverageReporter
from shared.contracts import (
    CoverageState,
    FaultCategory,
    FaultSpec,
    RegressionScenario,
    RegressionSuite,
    SafetyConstraints,
)


def _fault(name: str, category: FaultCategory) -> FaultSpec:
    return FaultSpec(
        category=category,
        name=name,
        target_selector={"app": "x"},
        duration_seconds=1,
        rationale="r",
    )


def _suite() -> RegressionSuite:
    return RegressionSuite(
        name="s",
        target_app="app",
        safety=SafetyConstraints(cluster_context="kind-test", namespace="default"),
        scenarios=[
            RegressionScenario(
                title="cart survives pod kill",
                fault=_fault("pod.kill", FaultCategory.POD),
                journeys=["cart:add"],
            )
        ],
        all_journeys=["cart:add", "checkout:pay"],
    )


def test_covered_vs_unknown_with_fault_filter() -> None:
    matrix = CoverageReporter().render(_suite(), faults=["pod.kill"])

    assert matrix.faults == ["pod.kill"]
    assert matrix.journeys == ["cart:add", "checkout:pay"]

    by_key = {(c.fault, c.journey): c for c in matrix.cells}
    assert by_key[("pod.kill", "cart:add")].state == CoverageState.COVERED
    assert by_key[("pod.kill", "cart:add")].scenario_id is not None
    assert by_key[("pod.kill", "checkout:pay")].state == CoverageState.UNKNOWN

    assert matrix.covered == 1
    assert matrix.gaps == 1  # the UNKNOWN cell counts as a gap
    assert matrix.na == 0  # v1 never fabricates NA
    assert matrix.comprehensiveness == 0.5


def test_default_axis_scoped_to_used_categories() -> None:
    matrix = CoverageReporter().render(_suite())
    # Default axis = catalogue faults in the suite's used categories (POD only),
    # NOT the whole catalogue — so unrelated hardware faults don't tank the score.
    assert set(matrix.faults) == {"pod.kill", "pod.failure"}
    assert "wifi.deauth" not in matrix.faults  # different category, excluded
    assert matrix.covered == 1


def test_empty_suite_comprehensiveness_is_none() -> None:
    suite = RegressionSuite(
        name="empty",
        target_app="app",
        safety=SafetyConstraints(cluster_context="kind-test", namespace="default"),
        scenarios=[],
        all_journeys=[],
    )
    matrix = CoverageReporter().render(suite)
    # No relevant cells -> comprehensiveness is n/a, never a misleading 100%.
    assert matrix.comprehensiveness is None
