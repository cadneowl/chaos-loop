"""
Prometheus query backend.

Two implementations:
    - HttpxPromBackend: real, hits the Prometheus HTTP API
    - FixturePromBackend: returns canned data; used by tests

The PromBackend Protocol is what the tester agent depends on. Swap implementations
without touching the agent.

Prometheus HTTP API reference:
    /api/v1/query              -- instant query
    /api/v1/query_range        -- range query

Both endpoints return a result like:
    {"status": "success", "data": {"resultType": "vector|matrix", "result": [...]}}
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx


class PromQueryError(RuntimeError):
    """Raised when a Prometheus query fails to execute or returns a non-success status."""


@dataclass(frozen=True)
class InstantSample:
    """A single (timestamp, value) point. timestamp is unix seconds."""

    timestamp: float
    value: float
    labels: dict[str, str]


class PromBackend(Protocol):
    """Minimal Prometheus query interface used by tester probes."""

    async def query_instant(self, query: str, *, ts: float | None = None) -> list[InstantSample]:
        """Run a PromQL instant query. If ts is None, use the server's current time."""

    async def query_range(
        self,
        query: str,
        *,
        start: float,
        end: float,
        step_seconds: float,
    ) -> list[list[InstantSample]]:
        """Run a PromQL range query. Returns one series of points per matching label set."""


# ---------------------------------------------------------------------------- #
# Real backend                                                                 #
# ---------------------------------------------------------------------------- #


class HttpxPromBackend:
    """Real Prometheus backend using httpx. Async."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Use shared client when provided (lets callers reuse connections);
        # otherwise we create a fresh one per query (simpler, slightly slower).
        self._client = client

    @classmethod
    def from_env(cls, var: str = "PROM_URL", default: str | None = None) -> HttpxPromBackend:
        url = os.environ.get(var) or default
        if not url:
            raise PromQueryError(
                f"No Prometheus URL configured. Set ${var} or pass explicitly."
            )
        return cls(url)

    async def query_instant(self, query: str, *, ts: float | None = None) -> list[InstantSample]:
        params: dict[str, str] = {"query": query}
        if ts is not None:
            params["time"] = str(ts)
        data = await self._get("/api/v1/query", params)
        return _parse_instant(data)

    async def query_range(
        self,
        query: str,
        *,
        start: float,
        end: float,
        step_seconds: float,
    ) -> list[list[InstantSample]]:
        params = {
            "query": query,
            "start": str(start),
            "end": str(end),
            "step": str(step_seconds),
        }
        data = await self._get("/api/v1/query_range", params)
        return _parse_range(data)

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        url = self.base_url + path
        if self._client is not None:
            resp = await self._client.get(url, params=params, timeout=self.timeout)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                resp = await c.get(url, params=params)
        if resp.status_code != 200:
            raise PromQueryError(f"Prometheus HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("status") != "success":
            raise PromQueryError(f"Prometheus query failed: {payload.get('error', payload)}")
        return payload.get("data", {})


# ---------------------------------------------------------------------------- #
# Fixture backend (for tests)                                                  #
# ---------------------------------------------------------------------------- #


class FixturePromBackend:
    """
    Fixture-driven Prometheus backend.

    Construct with a dict mapping (query_string, mode) -> response payload.
    Mode is "instant" or "range". Unknown queries raise PromQueryError unless
    a default response is provided.

    Example:
        backend = FixturePromBackend({
            ("up{job=\"foo\"}", "instant"): [
                {"value": [1620000000, "1"], "labels": {"job": "foo"}}
            ],
        })
    """

    def __init__(
        self,
        fixtures: dict[tuple[str, str], list[dict]] | None = None,
        *,
        default_instant: list[dict] | None = None,
    ) -> None:
        self._fixtures = fixtures or {}
        self._default_instant = default_instant

    def set(self, query: str, mode: str, response: list[dict]) -> None:
        self._fixtures[(query, mode)] = response

    async def query_instant(self, query: str, *, ts: float | None = None) -> list[InstantSample]:
        key = (query, "instant")
        if key in self._fixtures:
            return [_dict_to_sample(p) for p in self._fixtures[key]]
        if self._default_instant is not None:
            return [_dict_to_sample(p) for p in self._default_instant]
        raise PromQueryError(f"No fixture for instant query: {query!r}")

    async def query_range(
        self,
        query: str,
        *,
        start: float,
        end: float,
        step_seconds: float,
    ) -> list[list[InstantSample]]:
        key = (query, "range")
        if key in self._fixtures:
            return [[_dict_to_sample(p) for p in series] for series in self._fixtures[key]]
        raise PromQueryError(f"No fixture for range query: {query!r}")


# ---------------------------------------------------------------------------- #
# Parsers                                                                      #
# ---------------------------------------------------------------------------- #


def _parse_instant(data: dict) -> list[InstantSample]:
    """Parse a Prometheus instant-query response (`resultType: vector`)."""
    result = data.get("result", [])
    return [_pt_to_sample(item.get("value", [0, "0"]), item.get("metric", {})) for item in result]


def _parse_range(data: dict) -> list[list[InstantSample]]:
    """Parse a Prometheus range-query response (`resultType: matrix`)."""
    result = data.get("result", [])
    out: list[list[InstantSample]] = []
    for item in result:
        labels = item.get("metric", {})
        series = [_pt_to_sample(pt, labels) for pt in item.get("values", [])]
        out.append(series)
    return out


def _pt_to_sample(point: list, labels: dict[str, str]) -> InstantSample:
    # Prometheus returns [unix_ts: float, value: str]
    ts = float(point[0])
    raw_val = point[1]
    # "NaN" / "+Inf" / "-Inf" are valid Prometheus values; treat NaN as missing.
    try:
        value = float(raw_val)
    except (TypeError, ValueError):
        value = float("nan")
    return InstantSample(timestamp=ts, value=value, labels=dict(labels))


def _dict_to_sample(d: dict) -> InstantSample:
    """For FixturePromBackend: accept either {value: [ts, "v"], labels: {...}} or InstantSample-shaped."""
    if "value" in d and isinstance(d["value"], list):
        return _pt_to_sample(d["value"], d.get("labels", {}))
    return InstantSample(
        timestamp=float(d["timestamp"]),
        value=float(d["value"]),
        labels=dict(d.get("labels", {})),
    )
