"""
Probe — one query + an expectation about its result.

A probe captures: "this metric, under this query, should look like this when healthy."
Probes are declarative (YAML), reusable across targets, and evaluated mechanically
so baseline / verify don't need an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from agents.tester.tools.prometheus import InstantSample, PromBackend, PromQueryError

# ---------------------------------------------------------------------------- #
# Schema                                                                       #
# ---------------------------------------------------------------------------- #


class ProbeExpectation(BaseModel):
    """How to decide whether the result of a probe indicates a healthy state."""

    kind: Literal["value_below", "value_above", "value_between", "result_not_empty"]
    threshold: float | None = Field(
        default=None, description="for value_below / value_above"
    )
    minimum: float | None = Field(default=None, description="for value_between")
    maximum: float | None = Field(default=None, description="for value_between")


class Probe(BaseModel):
    """One probe: a PromQL query plus what 'healthy' looks like."""

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    query: str = Field(min_length=1)
    mode: Literal["instant"] = "instant"  # range mode lands in M2.1
    expect: ProbeExpectation
    metric_name: str = Field(
        description="Identifier used as StatisticalSample.metric in the report"
    )


# ---------------------------------------------------------------------------- #
# Evaluation                                                                   #
# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of evaluating one probe."""

    probe: Probe
    samples: list[float]
    passed: bool
    reason: str = ""


def _aggregate(samples: list[InstantSample]) -> float | None:
    """Pick a single representative value from a vector result.

    For now: if there's exactly one series, return its value.
    If multiple, sum them (common case: ratios already aggregated server-side).
    If empty, None.

    This is deliberately conservative — probes that want different aggregation
    should encode it in the PromQL.
    """
    if not samples:
        return None
    return sum(s.value for s in samples)


def _check_expectation(expect: ProbeExpectation, value: float) -> tuple[bool, str]:
    if expect.kind == "value_below":
        if expect.threshold is None:
            return False, "value_below requires threshold"
        return (value < expect.threshold, f"value {value} >= threshold {expect.threshold}")
    if expect.kind == "value_above":
        if expect.threshold is None:
            return False, "value_above requires threshold"
        return (value > expect.threshold, f"value {value} <= threshold {expect.threshold}")
    if expect.kind == "value_between":
        if expect.minimum is None or expect.maximum is None:
            return False, "value_between requires minimum and maximum"
        ok = expect.minimum <= value <= expect.maximum
        return (ok, f"value {value} outside [{expect.minimum}, {expect.maximum}]")
    if expect.kind == "result_not_empty":
        # Reaching here means we had at least one sample (non-empty), so pass.
        return True, ""
    return False, f"unknown expectation kind: {expect.kind}"


async def evaluate_probe(probe: Probe, backend: PromBackend) -> ProbeResult:
    """Run a probe against a backend and decide pass/fail."""
    try:
        samples = await backend.query_instant(probe.query)
    except PromQueryError as e:
        return ProbeResult(probe=probe, samples=[], passed=False, reason=f"query error: {e}")

    if not samples:
        # Empty result — pass only if the probe explicitly allows it.
        if probe.expect.kind == "result_not_empty":
            return ProbeResult(probe=probe, samples=[], passed=False, reason="empty result")
        # For value_below / value_above / value_between, no data == probe can't evaluate.
        return ProbeResult(probe=probe, samples=[], passed=False, reason="no samples returned")

    aggregated = _aggregate(samples)
    if aggregated is None:
        return ProbeResult(probe=probe, samples=[], passed=False, reason="aggregation produced None")

    if probe.expect.kind == "result_not_empty":
        return ProbeResult(probe=probe, samples=[aggregated], passed=True)

    passed, reason = _check_expectation(probe.expect, aggregated)
    return ProbeResult(probe=probe, samples=[aggregated], passed=passed, reason="" if passed else reason)
