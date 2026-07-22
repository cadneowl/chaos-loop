"""Build the fault-by-journey coverage matrix.

The journey axis is the suite's ``all_journeys``. The fault axis defaults to the
catalogue faults **in the categories the suite actually exercises** — not the
whole catalogue, so a web suite's comprehensiveness isn't dragged to ~2% by
hardware faults (wifi/power/sensor) it will never run. Pass ``faults=[...]`` to
scope the axis explicitly.

A cell is:

* **COVERED** — a scenario pairs that fault (by ``FaultSpec.name``) with the journey.
* **NA** — provably not-applicable: the fault only ever targets services (per the
  suite's scenarios) that the journey never traverses. Requires ``footprints``
  (see ``regression.relevance``) and always carries the evidence that backs it.
* **UNKNOWN** — everything else; counts as a gap. We never claim NA without proof.
"""

from __future__ import annotations

from collections.abc import Mapping

from agents.chaos.faults._meta import CATALOGUE
from regression.relevance import suite_fault_targets
from shared.contracts import (
    CoverageCell,
    CoverageMatrix,
    CoverageState,
    RegressionSuite,
)


class CoverageReporter:
    def render(
        self,
        suite: RegressionSuite,
        *,
        faults: list[str] | None = None,
        footprints: Mapping[str, set[str]] | None = None,
    ) -> CoverageMatrix:
        if faults:
            fault_axis = sorted(faults)
        else:
            # Default to the catalogue faults in the categories this suite uses.
            used_categories = {s.fault.category for s in suite.scenarios}
            fault_axis = sorted(
                name
                for name, defn in CATALOGUE.items()
                if defn.category in used_categories
            )
        journeys = list(suite.all_journeys)

        # (fault name, journey) -> covering scenario id.
        covered: dict[tuple[str, str], str] = {}
        for scenario in suite.scenarios:
            for journey in scenario.journeys:
                covered[(scenario.fault.name, journey)] = scenario.scenario_id

        fault_targets = suite_fault_targets(suite)
        fp = footprints or {}

        cells: list[CoverageCell] = []
        for fault in fault_axis:
            for journey in journeys:
                cells.append(
                    self._cell(fault, journey, covered, fault_targets, fp)
                )

        return CoverageMatrix(
            suite_id=suite.suite_id, faults=fault_axis, journeys=journeys, cells=cells
        )

    def _cell(
        self,
        fault: str,
        journey: str,
        covered: dict[tuple[str, str], str],
        fault_targets: dict[str, set[str]],
        footprints: Mapping[str, set[str]],
    ) -> CoverageCell:
        scenario_id = covered.get((fault, journey))
        if scenario_id is not None:
            return CoverageCell(
                fault=fault,
                journey=journey,
                state=CoverageState.COVERED,
                scenario_id=scenario_id,
            )

        # Provably not-applicable: we know this journey's footprint, the fault is
        # bound to target service(s) by the suite, and none of them are on the
        # journey's path. Never NA without that evidence.
        journey_services = footprints.get(journey)
        targets = fault_targets.get(fault, set())
        if journey_services is not None and targets and targets.isdisjoint(journey_services):
            return CoverageCell(
                fault=fault,
                journey=journey,
                state=CoverageState.NA,
                evidence={
                    "reason": "fault targets only services this journey never traverses",
                    "fault_targets": sorted(targets),
                    "journey_services": sorted(journey_services),
                },
            )

        return CoverageCell(fault=fault, journey=journey, state=CoverageState.UNKNOWN)
