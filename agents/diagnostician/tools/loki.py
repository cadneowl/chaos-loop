"""
Loki log query backend.

Same shape as the tester's Prometheus backend: Protocol + Httpx + Fixture
implementations so the diagnostician is unit-testable against canned data.

Loki HTTP API reference:
    /loki/api/v1/query_range  -- range query (returns matrix of log streams)
    /loki/api/v1/query        -- instant query (rarely useful for logs)

A range-query response shape (simplified):
    {
      "status": "success",
      "data": {
        "resultType": "streams",
        "result": [
          {
            "stream": {"service": "cart", "level": "error"},
            "values": [
              ["<unix_ns_ts>", "log line text"],
              ...
            ]
          },
          ...
        ]
      }
    }
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from agents._retry import async_retry

# Transient HTTP errors worth retrying. 4xx body errors are NOT in here — those
# are caller-side bugs we don't want to mask.
_RETRYABLE_HTTPX = (httpx.TransportError, httpx.TimeoutException)


class LokiQueryError(RuntimeError):
    """Raised when a Loki query fails to execute or returns a non-success status."""


@dataclass(frozen=True)
class LogLine:
    """One log line returned by Loki."""

    timestamp_ns: int  # Loki returns nanoseconds since epoch
    line: str
    labels: dict[str, str]


class LokiBackend(Protocol):
    """Minimal Loki query interface used by the diagnostician."""

    async def query_range(
        self,
        logql: str,
        *,
        start: float,
        end: float,
        limit: int = 1000,
    ) -> list[LogLine]:
        """Range query. start / end are unix seconds (float)."""


# ---------------------------------------------------------------------------- #
# Real backend                                                                 #
# ---------------------------------------------------------------------------- #


class HttpxLokiBackend:
    """Real Loki backend using httpx."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client

    @classmethod
    def from_env(cls, var: str = "LOKI_URL", default: str | None = None) -> HttpxLokiBackend:
        url = os.environ.get(var) or default
        if not url:
            raise LokiQueryError(
                f"No Loki URL configured. Set ${var} or pass explicitly."
            )
        return cls(url)

    async def query_range(
        self,
        logql: str,
        *,
        start: float,
        end: float,
        limit: int = 1000,
    ) -> list[LogLine]:
        # Loki expects start/end as RFC3339 or unix nanoseconds; we use nanoseconds.
        params = {
            "query": logql,
            "start": str(int(start * 1_000_000_000)),
            "end": str(int(end * 1_000_000_000)),
            "limit": str(limit),
            "direction": "forward",
        }
        data = await self._get("/loki/api/v1/query_range", params)
        return _parse_streams(data)

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        url = self.base_url + path

        async def _do_request() -> httpx.Response:
            if self._client is not None:
                return await self._client.get(url, params=params, timeout=self.timeout)
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                return await c.get(url, params=params)

        # Retry on transport / timeout errors. Non-transient errors (4xx body
        # decoded later, or invalid URL) bubble up untouched.
        resp = await async_retry(_do_request, max_attempts=3, retry_on=_RETRYABLE_HTTPX)
        if resp.status_code != 200:
            raise LokiQueryError(f"Loki HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("status") != "success":
            raise LokiQueryError(f"Loki query failed: {payload.get('error', payload)}")
        return payload.get("data", {})


# ---------------------------------------------------------------------------- #
# Fixture backend                                                              #
# ---------------------------------------------------------------------------- #


class FixtureLokiBackend:
    """
    Fixture-driven Loki backend.

    Construct with a dict mapping logql -> list of {labels: {...}, lines: [(ts_ns, "text"), ...]}.
    """

    def __init__(self, fixtures: dict[str, list[dict]] | None = None) -> None:
        self._fixtures = fixtures or {}

    def set(self, logql: str, streams: list[dict]) -> None:
        self._fixtures[logql] = streams

    async def query_range(
        self,
        logql: str,
        *,
        start: float,
        end: float,
        limit: int = 1000,
    ) -> list[LogLine]:
        if logql not in self._fixtures:
            raise LokiQueryError(f"No fixture for LogQL: {logql!r}")
        start_ns = int(start * 1_000_000_000)
        end_ns = int(end * 1_000_000_000)
        out: list[LogLine] = []
        for stream in self._fixtures[logql]:
            labels = dict(stream.get("labels", {}))
            for ts_ns, line in stream.get("lines", []):
                if start_ns <= ts_ns <= end_ns:
                    out.append(LogLine(timestamp_ns=int(ts_ns), line=str(line), labels=labels))
                    if len(out) >= limit:
                        return out
        return out


# ---------------------------------------------------------------------------- #
# Parser                                                                       #
# ---------------------------------------------------------------------------- #


def _parse_streams(data: dict) -> list[LogLine]:
    """Parse a Loki query_range response (`resultType: streams`)."""
    out: list[LogLine] = []
    for stream in data.get("result", []):
        labels = stream.get("stream", {})
        for entry in stream.get("values", []):
            ts_ns_str, line = entry[0], entry[1]
            out.append(LogLine(timestamp_ns=int(ts_ns_str), line=str(line), labels=dict(labels)))
    return out
