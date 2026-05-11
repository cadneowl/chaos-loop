"""Unit tests for the tester agent's baseline + verify, using a fixture backend."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.tester.agent import ClaudeTesterAgent
from agents.tester.probes import (
    Probe,
    ProbeExpectation,
    evaluate_probe,
    load_probe_set,
    probes_for_target,
)
from agents.tester.tools.prometheus import FixturePromBackend
from shared.contracts import TesterRequest

# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _value(value: float, *, labels: dict[str, str] | None = None) -> dict:
    """Helper to build a fixture point in Prometheus instant-query format."""
    return {"value": [1620000000, str(value)], "labels": labels or {}}


def _agent_with(
    backend: FixturePromBackend, *, probes_dir: Path | None = None
) -> ClaudeTesterAgent:
    return ClaudeTesterAgent(prom_backend=backend, probes_dir=probes_dir)


def _req(target: str = "synthetic", runs: int = 1) -> TesterRequest:
    return TesterRequest(
        kind="baseline",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app=target,
        baseline_run_count=runs,
    )


# ---------------------------------------------------------------------------- #
# Probe evaluation primitives                                                  #
# ---------------------------------------------------------------------------- #


def test_value_below_passes_when_under_threshold() -> None:
    probe = Probe(
        name="latency_ok",
        query="latency_ms",
        expect=ProbeExpectation(kind="value_below", threshold=500),
        metric_name="latency_ms",
    )
    backend = FixturePromBackend({("latency_ms", "instant"): [_value(120)]})
    result = asyncio.run(evaluate_probe(probe, backend))
    assert result.passed
    assert result.samples == [120.0]


def test_value_below_fails_at_threshold() -> None:
    probe = Probe(
        name="latency_too_high",
        query="latency_ms",
        expect=ProbeExpectation(kind="value_below", threshold=500),
        metric_name="latency_ms",
    )
    backend = FixturePromBackend({("latency_ms", "instant"): [_value(750)]})
    result = asyncio.run(evaluate_probe(probe, backend))
    assert not result.passed
    assert "750" in result.reason


def test_result_not_empty_fails_on_empty() -> None:
    probe = Probe(
        name="services_up",
        query="up",
        expect=ProbeExpectation(kind="result_not_empty"),
        metric_name="services_up",
    )
    backend = FixturePromBackend({("up", "instant"): []})
    result = asyncio.run(evaluate_probe(probe, backend))
    assert not result.passed


def test_result_not_empty_passes_with_samples() -> None:
    probe = Probe(
        name="services_up",
        query="up",
        expect=ProbeExpectation(kind="result_not_empty"),
        metric_name="services_up",
    )
    backend = FixturePromBackend({("up", "instant"): [_value(1), _value(1)]})
    result = asyncio.run(evaluate_probe(probe, backend))
    assert result.passed


def test_query_error_marks_probe_failed() -> None:
    """If the backend has no fixture for a query, evaluate_probe surfaces the error."""
    probe = Probe(
        name="missing",
        query="this_query_has_no_fixture",
        expect=ProbeExpectation(kind="result_not_empty"),
        metric_name="missing",
    )
    backend = FixturePromBackend()
    result = asyncio.run(evaluate_probe(probe, backend))
    assert not result.passed
    assert "query error" in result.reason


# ---------------------------------------------------------------------------- #
# Probe loader                                                                 #
# ---------------------------------------------------------------------------- #


def test_load_synthetic_probes() -> None:
    probes = probes_for_target("synthetic")
    names = [p.name for p in probes]
    assert "prometheus_self_up" in names
    assert "scrape_duration_p95_ms" in names


def test_load_otel_demo_probes() -> None:
    probes = probes_for_target("otel-demo")
    assert any(p.name == "frontend_5xx_rate" for p in probes)
    # all probes should validate
    assert all(p.query for p in probes)


def test_unknown_target_raises() -> None:
    with pytest.raises(FileNotFoundError):
        probes_for_target("does-not-exist")


def test_load_probe_set_from_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "custom.yaml"
    yaml_path.write_text(
        """
probes:
  - name: p1
    description: test
    query: vector(1)
    mode: instant
    expect:
      kind: result_not_empty
    metric_name: p1
"""
    )
    probes = load_probe_set(yaml_path)
    assert len(probes) == 1
    assert probes[0].name == "p1"


# ---------------------------------------------------------------------------- #
# Agent.baseline integration (via fixture backend)                             #
# ---------------------------------------------------------------------------- #


def test_baseline_synthetic_healthy() -> None:
    backend = FixturePromBackend({
        ('up{job="prometheus"}', "instant"): [_value(1, labels={"job": "prometheus"})],
        ("quantile(0.95, scrape_duration_seconds) * 1000", "instant"): [_value(45.0)],
    })
    agent = _agent_with(backend)
    report = asyncio.run(agent.baseline(_req("synthetic")))

    assert report.steady_state, f"failed_probes={report.failed_probes}, anomalies={report.anomalies}"
    assert report.failed_probes == []
    assert report.request_kind == "baseline"
    assert {s.metric for s in report.samples} == {
        "prometheus_self_up",
        "scrape_duration_p95_ms",
    }


def test_baseline_marks_regression_when_threshold_exceeded() -> None:
    backend = FixturePromBackend({
        ('up{job="prometheus"}', "instant"): [_value(1)],
        # scrape_duration is over threshold of 1000ms
        ("quantile(0.95, scrape_duration_seconds) * 1000", "instant"): [_value(2500.0)],
    })
    agent = _agent_with(backend)
    report = asyncio.run(agent.baseline(_req("synthetic")))

    assert not report.steady_state
    assert "scrape_duration_p95_ms" in report.failed_probes
    assert any("2500" in a for a in report.anomalies)


def test_baseline_unknown_target_is_unsteady() -> None:
    agent = _agent_with(FixturePromBackend())
    report = asyncio.run(agent.baseline(_req("nonexistent-target")))
    assert not report.steady_state
    assert any("no probe set" in a for a in report.anomalies)


def test_baseline_n_runs_record_n_samples() -> None:
    """When runs=3, each probe should yield 3 raw samples."""
    backend = FixturePromBackend({
        ('up{job="prometheus"}', "instant"): [_value(1)],
        ("quantile(0.95, scrape_duration_seconds) * 1000", "instant"): [_value(45.0)],
    })
    agent = _agent_with(backend)
    report = asyncio.run(agent.baseline(_req("synthetic", runs=3)))
    for s in report.samples:
        assert len(s.samples) == 3, f"{s.metric} had {len(s.samples)} samples, expected 3"


# ---------------------------------------------------------------------------- #
# Verify mode + baseline comparison                                            #
# ---------------------------------------------------------------------------- #


def _healthy_synthetic_backend(prom_value: float = 45.0) -> FixturePromBackend:
    return FixturePromBackend({
        ('up{job="prometheus"}', "instant"): [_value(1)],
        ("quantile(0.95, scrape_duration_seconds) * 1000", "instant"): [_value(prom_value)],
    })


def test_verify_steady_when_no_baseline_provided() -> None:
    """No baseline -> only probe expectations are evaluated; same as baseline mode."""
    agent = _agent_with(_healthy_synthetic_backend())
    req = TesterRequest(
        kind="verify",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app="synthetic",
    )
    report = asyncio.run(agent.verify(req))
    assert report.steady_state
    assert report.request_kind == "verify"


def test_verify_flags_shift_against_baseline() -> None:
    """Mean shifted 5 sigma from baseline -> anomaly even if probe expectation passes."""
    from shared.contracts import StatisticalSample

    # Baseline: mean=50, stdev=10 (a nice tight distribution).
    baseline = StatisticalSample.from_samples(
        metric="scrape_duration_p95_ms",
        samples=[40, 45, 50, 55, 60],
    )
    assert baseline.stdev > 0
    # New observation 150ms — well above the baseline's 50±10ms and still below
    # the probe's absolute threshold of 1000ms, so probe-expect alone would pass.
    agent = _agent_with(_healthy_synthetic_backend(prom_value=150.0))
    req = TesterRequest(
        kind="verify",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app="synthetic",
        baseline_samples=[baseline],
    )
    report = asyncio.run(agent.verify(req))
    assert not report.steady_state
    # No probe should be in failed_probes (expect still satisfied)
    assert "scrape_duration_p95_ms" not in [p for p in report.failed_probes]
    # But there should be an anomaly mentioning the shift.
    assert any("scrape_duration_p95_ms" in a and "sigma" in a for a in report.anomalies)


def test_verify_no_shift_within_baseline() -> None:
    """New value close to baseline mean -> no shift anomaly."""
    from shared.contracts import StatisticalSample

    baseline = StatisticalSample.from_samples(
        metric="scrape_duration_p95_ms",
        samples=[40, 45, 50, 55, 60],
    )
    agent = _agent_with(_healthy_synthetic_backend(prom_value=48.0))
    req = TesterRequest(
        kind="verify",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app="synthetic",
        baseline_samples=[baseline],
    )
    report = asyncio.run(agent.verify(req))
    assert report.steady_state


def test_verify_skips_comparison_when_baseline_has_zero_stdev() -> None:
    """A single-sample baseline (stdev=0) can't be Z-tested; we skip without erroring."""
    from shared.contracts import StatisticalSample

    baseline = StatisticalSample.from_samples(
        metric="scrape_duration_p95_ms",
        samples=[50.0],
    )
    assert baseline.stdev == 0
    agent = _agent_with(_healthy_synthetic_backend(prom_value=500.0))
    req = TesterRequest(
        kind="verify",
        experiment_id="exp-aaaaaaaaaaaa",
        target_app="synthetic",
        baseline_samples=[baseline],
    )
    report = asyncio.run(agent.verify(req))
    # Probe expect (below 1000ms) still passes; no statistical shift flag because
    # we can't compute z without dispersion.
    assert report.steady_state


def test_baseline_records_anomaly_when_one_probe_fails() -> None:
    """If one probe fails but another passes, report names the failing one."""
    backend = FixturePromBackend({
        ('up{job="prometheus"}', "instant"): [],  # empty -> probe with result_not_empty fails
        ("quantile(0.95, scrape_duration_seconds) * 1000", "instant"): [_value(45.0)],
    })
    agent = _agent_with(backend)
    report = asyncio.run(agent.baseline(_req("synthetic")))
    assert not report.steady_state
    assert report.failed_probes == ["prometheus_self_up"]
    # The healthy probe should still produce a sample.
    assert any(s.metric == "scrape_duration_p95_ms" for s in report.samples)
