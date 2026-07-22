"""Relevance: promote UNKNOWN coverage gaps to evidence-backed NA.

A journey's *footprint* is the set of services it traverses. A ``(fault,
journey)`` cell is provably **not-applicable** when the fault — as bound to
target services by the suite's own scenarios — only ever hits services the
journey never touches. That is the one honest way to shrink the "relevant"
denominator: we never mark a cell NA without the trace/graph fact that backs it.

Footprints come from a pluggable ``RelevanceSource``:

* ``DeclarativeRelevanceSource`` — an authored or observed ``{journey: [service]}``
  map (offline, no infra). What ``chaos regression coverage --footprints`` uses.
* ``TraceRelevanceSource`` — derived from distributed traces (Tempo / Jaeger):
  the service set per journey, via a mockable ``TraceClient``. The concrete
  backend client is the remaining wiring; the shape and classification are done.

The reporter stays pure: a source resolves a ``{journey: set[service]}`` mapping,
which is handed to ``CoverageReporter.render(..., footprints=...)``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from shared.contracts import FaultSpec, RegressionSuite

log = logging.getLogger(__name__)


def fault_target_services(fault: FaultSpec) -> set[str]:
    """The service identifiers a fault targets — the values of its label selector.

    e.g. ``{app.kubernetes.io/component: valkey-cart}`` -> ``{"valkey-cart"}``.
    """
    return {str(v) for v in fault.target_selector.values() if v}


def suite_fault_targets(suite: RegressionSuite) -> dict[str, set[str]]:
    """Fault name -> union of services the suite's scenarios target with it.

    A fault the suite never binds to a target has no entry, so its cells can
    never be proven NA (we can't know where it would be injected).
    """
    targets: dict[str, set[str]] = {}
    for scenario in suite.scenarios:
        targets.setdefault(scenario.fault.name, set()).update(
            fault_target_services(scenario.fault)
        )
    return targets


class RelevanceSource(Protocol):
    async def footprints(self, journeys: list[str]) -> dict[str, set[str]]:
        """Journey id -> services it traverses. Omit a journey to leave it unknown."""
        ...


class DeclarativeRelevanceSource:
    """Footprints from an authored/observed ``{journey: [service]}`` mapping."""

    def __init__(self, mapping: Mapping[str, list[str]]) -> None:
        self._map = {j: {str(s) for s in svcs} for j, svcs in mapping.items()}

    async def footprints(self, journeys: list[str]) -> dict[str, set[str]]:
        return {j: set(self._map[j]) for j in journeys if j in self._map}


class TraceClient(Protocol):
    async def services_for_journey(self, journey: str) -> list[str]:
        """Services observed in the journey's distributed trace. Raise if unavailable."""
        ...


class TraceRelevanceSource:
    """Footprints derived from distributed traces via a ``TraceClient``.

    A journey whose trace can't be fetched is left *unknown* (not NA) — absence
    of a trace is never treated as evidence of irrelevance.
    """

    def __init__(self, client: TraceClient) -> None:
        self._client = client

    async def footprints(self, journeys: list[str]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for journey in journeys:
            try:
                services = await self._client.services_for_journey(journey)
            except Exception as e:
                log.warning("trace lookup failed for %s: %r", journey, e)
                continue
            if services:
                out[journey] = {str(s) for s in services}
        return out
