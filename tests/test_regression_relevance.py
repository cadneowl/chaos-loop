"""Trace-based relevance: footprint sources + evidence-backed NA classification.

The rule: a (fault, journey) cell is NA only when the fault is bound to target
service(s) by the suite AND the journey provably never traverses them. No
footprint, or an intersecting one, or a fault with no scenario target => never NA.
"""

from __future__ import annotations

import pytest

from regression.coverage import CoverageReporter
from regression.relevance import (
    DeclarativeRelevanceSource,
    FootprintError,
    TraceRelevanceSource,
    check_footprints,
    fault_target_services,
    parse_footprints,
    suite_fault_targets,
)
from shared.contracts import (
    CoverageCell,
    CoverageMatrix,
    CoverageState,
    FaultCategory,
    FaultSpec,
    RegressionScenario,
    RegressionSuite,
    SafetyConstraints,
)


def _fault(name: str, target: dict[str, str]) -> FaultSpec:
    return FaultSpec(
        category=FaultCategory.NETWORK,
        name=name,
        target_selector=target,
        duration_seconds=10,
        rationale="r",
    )


def _suite() -> RegressionSuite:
    return RegressionSuite(
        name="s",
        target_app="app",
        safety=SafetyConstraints(cluster_context="kind-test", namespace="app"),
        scenarios=[
            RegressionScenario(
                title="cart survives redis loss",
                fault=_fault("network.loss", {"app": "redis"}),
                journeys=["cart:add"],
            )
        ],
        all_journeys=["cart:add", "browse:list"],
    )


# ----- sources --------------------------------------------------------------


async def test_declarative_source_omits_unknown_journeys() -> None:
    src = DeclarativeRelevanceSource({"a": ["s1", "s2"], "b": ["s3"]})
    assert await src.footprints(["a", "c"]) == {"a": {"s1", "s2"}}  # c unknown


async def test_trace_source_omits_journeys_whose_trace_errors() -> None:
    class FakeClient:
        async def services_for_journey(self, journey: str) -> list[str]:
            if journey == "boom":
                raise RuntimeError("no trace")
            return {"a": ["frontend", "redis"], "b": ["frontend"]}[journey]

    fp = await TraceRelevanceSource(FakeClient()).footprints(["a", "b", "boom"])
    assert fp == {"a": {"frontend", "redis"}, "b": {"frontend"}}  # boom left unknown


async def test_declarative_source_rejects_string_value() -> None:
    with pytest.raises(TypeError, match="must be a list"):
        DeclarativeRelevanceSource({"j": "frontend"})  # type: ignore[dict-item]


def test_fault_target_services_reads_selector_values() -> None:
    f = _fault("network.loss", {"app.kubernetes.io/component": "valkey-cart"})
    assert fault_target_services(f) == {"valkey-cart"}


# ----- footprints file validation -------------------------------------------


def test_parse_footprints_rejects_non_mapping() -> None:
    with pytest.raises(FootprintError, match="must be a YAML mapping"):
        parse_footprints(["a", "b"])


def test_parse_footprints_rejects_string_value() -> None:
    # The dangerous footgun: 'frontend' would iterate into {'f','r','o',...}.
    with pytest.raises(FootprintError, match="must be a list"):
        parse_footprints({"j": "frontend"})


def test_parse_footprints_normalizes_lists() -> None:
    assert parse_footprints({"j": ["a", "b"]}) == {"j": {"a", "b"}}


def test_check_footprints_raises_on_unknown_journey() -> None:
    with pytest.raises(FootprintError, match="not in the suite"):
        check_footprints({"typo:journey": {"s"}}, _suite())


def test_check_footprints_warns_on_total_name_mismatch() -> None:
    # Suite targets {redis}; these names share nothing with it.
    warnings = check_footprints(
        {"cart:add": {"cart-svc"}, "browse:list": {"catalog"}}, _suite()
    )
    assert any("no footprint service name matches" in w for w in warnings)


def test_check_footprints_no_warning_on_partial_match() -> None:
    warnings = check_footprints(
        {"cart:add": {"redis"}, "browse:list": {"catalog"}}, _suite()
    )
    assert warnings == []


def test_suite_fault_targets_unions_over_scenarios() -> None:
    assert suite_fault_targets(_suite()) == {"network.loss": {"redis"}}


# ----- classification -------------------------------------------------------


def _cells(matrix: CoverageMatrix) -> dict[tuple[str, str], CoverageCell]:
    return {(c.fault, c.journey): c for c in matrix.cells}


def test_disjoint_footprint_is_na_with_evidence() -> None:
    matrix = CoverageReporter().render(
        _suite(),
        faults=["network.loss"],
        footprints={"cart:add": {"frontend", "redis"}, "browse:list": {"frontend", "catalog"}},
    )
    cell = _cells(matrix)[("network.loss", "browse:list")]
    assert cell.state == CoverageState.NA
    assert cell.evidence["fault_targets"] == ["redis"]
    assert "redis" not in cell.evidence["journey_services"]
    # NA is excluded from the relevant denominator.
    assert matrix.na == 1
    assert matrix.comprehensiveness == 1.0


def test_intersecting_footprint_is_not_na() -> None:
    matrix = CoverageReporter().render(
        _suite(),
        faults=["network.loss"],
        footprints={"browse:list": {"frontend", "redis"}},  # browse touches redis
    )
    assert _cells(matrix)[("network.loss", "browse:list")].state == CoverageState.UNKNOWN


def test_fault_without_scenario_target_is_never_na() -> None:
    # network.delay is in the axis but no scenario binds it -> no known target ->
    # we can't prove irrelevance even with a footprint.
    matrix = CoverageReporter().render(
        _suite(),
        faults=["network.delay"],
        footprints={"browse:list": {"frontend"}},
    )
    assert _cells(matrix)[("network.delay", "browse:list")].state == CoverageState.UNKNOWN


def test_no_footprints_leaves_everything_unknown() -> None:
    matrix = CoverageReporter().render(_suite(), faults=["network.loss"])
    assert _cells(matrix)[("network.loss", "browse:list")].state == CoverageState.UNKNOWN
    assert matrix.na == 0
