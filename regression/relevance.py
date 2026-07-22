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


class FootprintError(ValueError):
    """A footprints map that is malformed or inconsistent with the suite.

    Fails loud on purpose: a silently-wrong footprint can turn a real coverage
    gap into a false ``n-a``, which is the worst error this tool can make.
    """


def parse_footprints(raw: object) -> dict[str, set[str]]:
    """Validate + normalize a footprints mapping ``{journey: [service, ...]}``.

    Rejects the common footguns: a non-mapping top level, and a bare **string**
    value (``journey: frontend``) — which would otherwise iterate into a set of
    single characters and produce nonsense relevance.
    """
    if not isinstance(raw, dict):
        raise FootprintError(
            "footprints must be a YAML mapping of journey -> [services], "
            f"got {type(raw).__name__}"
        )
    out: dict[str, set[str]] = {}
    for journey, services in raw.items():
        if isinstance(services, str) or not isinstance(services, list | tuple | set):
            raise FootprintError(
                f"footprints[{journey!r}] must be a list of service names, "
                f"got {type(services).__name__} ({services!r})"
            )
        out[str(journey)] = {str(s) for s in services}
    return out


def check_footprints(footprints: Mapping[str, set[str]], suite: RegressionSuite) -> list[str]:
    """Fail loud on unknown journeys; return warnings for likely name mismatches.

    * A footprint keyed on a journey not in ``all_journeys`` is a typo that would
      silently never apply -> ``FootprintError`` (same fail-loud stance as suites).
    * If **no** footprint service name matches **any** fault ``target_selector``
      value, that's a strong signal the two namespaces disagree (traces often use
      different service names than k8s selectors) — which yields false ``n-a``.
      Returned as a warning, since a partial match can still be legitimate.
    """
    unknown = sorted(set(footprints) - set(suite.all_journeys))
    if unknown:
        raise FootprintError(
            f"footprints reference journeys not in the suite's all_journeys "
            f"(typo, or missing from the suite?): {unknown}"
        )
    warnings: list[str] = []
    fp_services = {s for svcs in footprints.values() for s in svcs}
    targets = {s for tset in suite_fault_targets(suite).values() for s in tset}
    if fp_services and targets and fp_services.isdisjoint(targets):
        warnings.append(
            "no footprint service name matches any fault target_selector value "
            f"(targets: {sorted(targets)}; footprint services: {sorted(fp_services)}). "
            "Names must match — traces often use different service names than k8s "
            "selectors — otherwise every cell is falsely n-a."
        )
    return warnings


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
        self._map: dict[str, set[str]] = {}
        for journey, services in mapping.items():
            # Guard the string footgun even on the programmatic path.
            if isinstance(services, str):
                raise TypeError(
                    f"footprint for {journey!r} must be a list of services, not a string"
                )
            self._map[str(journey)] = {str(s) for s in services}

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
