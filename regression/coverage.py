"""Build the fault-by-journey coverage matrix.

The journey axis is the suite's ``all_journeys``. The fault axis defaults to the
catalogue faults **in the categories the suite actually exercises** — not the
whole catalogue, so a web suite's comprehensiveness isn't dragged to ~2% by
hardware faults (wifi/power/sensor) it will never run. Pass ``faults=[...]`` to
scope the axis explicitly.

A cell is COVERED when a scenario pairs that fault (by ``FaultSpec.name``) with
that journey. Everything else is UNKNOWN in v1 — the trace-based relevance
classifier that promotes UNKNOWN to a *provable* NA lands in v2, so v1 never
claims a cell is not-applicable without evidence.
"""

from __future__ import annotations

from agents.chaos.faults._meta import CATALOGUE
from shared.contracts import (
    CoverageCell,
    CoverageMatrix,
    CoverageState,
    RegressionSuite,
)


class CoverageReporter:
    def render(
        self, suite: RegressionSuite, *, faults: list[str] | None = None
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

        cells: list[CoverageCell] = []
        for fault in fault_axis:
            for journey in journeys:
                scenario_id = covered.get((fault, journey))
                if scenario_id is not None:
                    cells.append(
                        CoverageCell(
                            fault=fault,
                            journey=journey,
                            state=CoverageState.COVERED,
                            scenario_id=scenario_id,
                        )
                    )
                else:
                    cells.append(
                        CoverageCell(
                            fault=fault, journey=journey, state=CoverageState.UNKNOWN
                        )
                    )

        return CoverageMatrix(
            suite_id=suite.suite_id, faults=fault_axis, journeys=journeys, cells=cells
        )
