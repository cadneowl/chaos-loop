"""
HardwareTelemetryBackend — read tester probes from a HardwareIO instead of
Prometheus.

The Tester's `_run_probes` calls a `PromBackend` to evaluate each probe's
query. For NeoOwl probes the "query" isn't PromQL — it's just the metric
name the firmware exposes (`detector_latency_p95_ms`, `gateway_uplink_rtt_ms`,
…). This adapter wraps a `HardwareIO` and surfaces those reads as the
`InstantSample` shape the Tester already understands.

That means the Tester stays unchanged: we point its backend at a different
class, and probes evaluated against `agents/tester/probes/neoowl.yaml`
silently read from the bench instead of Prometheus.

Range queries are not supported (they imply a time-series store the
hardware bench doesn't keep on its own); the Tester only uses instant
queries for the steady-state checks Phase 1 needs, so a `NotImplementedError`
on range is acceptable for now.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.chaos.hardware_io import HardwareIO
from agents.tester.tools.prometheus import InstantSample, PromQueryError


@dataclass
class HardwareTelemetryBackend:
    """Implements `PromBackend` over a `HardwareIO`.

    `query` is interpreted as a literal metric name — no PromQL parsing.
    Probes that need server-side aggregation (`rate(...)`, `histogram_quantile(...)`)
    should be either pre-computed by the firmware or rewritten as plain
    metric reads when targeting hardware.
    """

    hardware: HardwareIO

    async def query_instant(
        self, query: str, *, ts: float | None = None
    ) -> list[InstantSample]:
        sample = await self.hardware.read_telemetry(query)
        return [
            InstantSample(
                timestamp=sample.timestamp,
                value=sample.value,
                labels=dict(sample.labels),
            )
        ]

    async def query_range(
        self,
        query: str,
        *,
        start: float,
        end: float,
        step_seconds: float,
    ) -> list[list[InstantSample]]:
        raise PromQueryError(
            "HardwareTelemetryBackend does not support range queries — "
            "use instant queries against firmware-computed metrics, or "
            "scrape the bench into a real Prometheus instance for time-series."
        )
