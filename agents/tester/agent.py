"""
Tester agent.

baseline() and verify() are pure Python — no LLM. The cognitive layer (Claude
Agent SDK) is reserved for hypothesize(), where the model reads the target's
source code and proposes fragility hypotheses. This keeps cost and latency low
in the hot loop, and steady_state determination is deterministic and explainable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console

from agents._cli import notice
from agents.tester.probes import (
    Probe,
    ProbeResult,
    evaluate_probe,
    probes_for_target,
)
from agents.tester.tools.prometheus import HttpxPromBackend, PromBackend
from shared.contracts import StatisticalSample, TesterReport, TesterRequest

_PROMPT_DIR = Path(__file__).parent / "prompts"

# Z-score threshold for flagging a metric as shifted from baseline. 3 = ~0.3%
# false-positive rate under a normal distribution. Conservative: we want false
# positives to be rare so the diagnostician's signal is high-precision.
_BASELINE_SHIFT_Z_THRESHOLD = 3.0


def _compare_to_baseline(
    new_samples: list[StatisticalSample],
    baseline_samples: list[StatisticalSample],
) -> list[str]:
    """Return one anomaly string per metric whose new mean shifted > 3 sigma from baseline.

    Baselines with zero stdev (single-sample distributions) are skipped — we can't
    do a Z-test without dispersion. M2.1 will populate stdev properly via N runs.
    """
    if not baseline_samples:
        return []
    by_metric = {b.metric: b for b in baseline_samples}
    anomalies: list[str] = []
    for sample in new_samples:
        b = by_metric.get(sample.metric)
        if b is None or b.stdev == 0:
            continue
        z = abs(sample.mean - b.mean) / b.stdev
        if z > _BASELINE_SHIFT_Z_THRESHOLD:
            anomalies.append(
                f"{sample.metric}: mean shifted {z:.1f} sigma from baseline "
                f"(baseline mean={b.mean:.3g}, now={sample.mean:.3g})"
            )
    return anomalies


class ClaudeTesterAgent:
    """Implements `orchestrator.loop.TesterAgent`."""

    def __init__(
        self,
        *,
        prom_backend: PromBackend | None = None,
        probes_dir: Path | None = None,
        model: str = "claude-opus-4-7",
    ) -> None:
        self._prom = prom_backend
        self._probes_dir = probes_dir
        self.model = model

    def _backend(self) -> PromBackend:
        if self._prom is None:
            # Resolve at use-time so tests can inject before any call.
            self._prom = HttpxPromBackend.from_env()
        return self._prom

    async def baseline(self, req: TesterRequest) -> TesterReport:
        """Run the target's probe set once and return a TesterReport."""
        return await self._run_probes(req, request_kind="baseline")

    async def verify(self, req: TesterRequest) -> TesterReport:
        """
        Verify mode. M2.0: re-runs the same probes the baseline would; no
        comparison-to-prior-distribution yet (that's M2.3). Steady-state is
        decided per-probe by the probe's own expectation.
        """
        return await self._run_probes(req, request_kind="verify")

    async def _run_probes(
        self,
        req: TesterRequest,
        *,
        request_kind: str,
    ) -> TesterReport:
        # 1. Load the probe set for this target.
        try:
            probes = probes_for_target(req.target_app, probes_dir=self._probes_dir)
        except FileNotFoundError as e:
            # Unknown target = configuration error, not steady. Surface it.
            return TesterReport(
                request_kind=request_kind,  # type: ignore[arg-type]
                experiment_id=req.experiment_id,
                steady_state=False,
                anomalies=[f"no probe set for target {req.target_app!r}: {e}"],
                notes="probe set missing",
            )

        # 2. Execute each probe `baseline_run_count` times (M2.0: single-shot;
        #    M2.1 will add inter-run delay + true N-run distribution).
        runs = req.baseline_run_count if request_kind == "baseline" else 1
        results: list[tuple[Probe, list[ProbeResult]]] = []
        backend = self._backend()
        for probe in probes:
            per_probe: list[ProbeResult] = []
            for _ in range(max(1, runs)):
                per_probe.append(await evaluate_probe(probe, backend))
                # M2.1 inserts an asyncio.sleep here between runs.
            results.append((probe, per_probe))

        # 3. Aggregate into StatisticalSample + decide steady_state.
        samples: list[StatisticalSample] = []
        failed_probes: list[str] = []
        anomalies: list[str] = []
        for probe, runs_for_probe in results:
            values = [v for r in runs_for_probe for v in r.samples]
            if values:
                samples.append(
                    StatisticalSample.from_samples(metric=probe.metric_name, samples=values)
                )
            any_failed = any(not r.passed for r in runs_for_probe)
            if any_failed:
                failed_probes.append(probe.name)
                # Surface the first failure reason; not every reason for every run.
                first_reason = next(
                    (r.reason for r in runs_for_probe if not r.passed and r.reason),
                    "probe failed",
                )
                anomalies.append(f"{probe.name}: {first_reason}")

        # 4. Statistical comparison vs baseline (verify mode only).
        # We flag any metric whose new mean is more than 3 standard deviations
        # from the baseline mean. This catches regressions that pass the probe's
        # absolute threshold but represent a meaningful shift in behavior.
        shifted = _compare_to_baseline(samples, req.baseline_samples)
        anomalies.extend(shifted)

        steady_state = not failed_probes and not shifted
        notes = f"ran {len(probes)} probe(s) x {max(1, runs)} run(s)"

        return TesterReport(
            request_kind=request_kind,  # type: ignore[arg-type]
            experiment_id=req.experiment_id,
            steady_state=steady_state,
            samples=samples,
            failed_probes=failed_probes,
            anomalies=anomalies,
            notes=notes,
        )


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(help="Tester agent — baseline, verify, and hypothesize.", no_args_is_help=True)
console = Console()


def _new_experiment_id() -> str:
    # Used by ad-hoc CLI invocations that aren't tied to an orchestrator run.
    from uuid import uuid4

    return f"exp-{uuid4().hex[:12]}"


@app.command()
def baseline(
    target: str = typer.Option(..., "--target", help="target_app identifier"),
    runs: int = typer.Option(5, "--runs", help="number of probe runs"),
    prom_url: str | None = typer.Option(
        None,
        "--prom-url",
        envvar="PROM_URL",
        help="Prometheus base URL (defaults to $PROM_URL)",
    ),
) -> None:
    """Establish a statistical baseline of healthy behavior."""
    if not prom_url:
        typer.echo(
            "error: no Prometheus URL configured. Pass --prom-url or set $PROM_URL.",
            err=True,
        )
        raise typer.Exit(code=2)
    agent = ClaudeTesterAgent(prom_backend=HttpxPromBackend(prom_url))
    req = TesterRequest(
        kind="baseline",
        experiment_id=_new_experiment_id(),
        target_app=target,
        baseline_run_count=runs,
    )
    report = asyncio.run(agent.baseline(req))
    console.print_json(json.dumps(report.model_dump(mode="json")))


@app.command()
def verify(
    target: str = typer.Option(..., "--target"),
    prom_url: str | None = typer.Option(None, "--prom-url", envvar="PROM_URL"),
) -> None:
    """Verify post-chaos behavior. M2.0: single-shot; baseline-comparison lands in M2.3."""
    if not prom_url:
        typer.echo("error: no Prometheus URL configured (--prom-url / $PROM_URL).", err=True)
        raise typer.Exit(code=2)
    agent = ClaudeTesterAgent(prom_backend=HttpxPromBackend(prom_url))
    req = TesterRequest(kind="verify", experiment_id=_new_experiment_id(), target_app=target)
    report = asyncio.run(agent.verify(req))
    console.print_json(json.dumps(report.model_dump(mode="json")))


@app.command()
def hypothesize(
    target_repo: str = typer.Option(..., "--target-repo", help="git URL of the target"),
) -> None:
    """Generate hypotheses by reading the target source code."""
    notice("tester", "hypothesize", "milestone-2.4",
           hint=f"would read {target_repo} and emit Hypothesis objects")


@app.command()
def replay(fixture: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Replay against a recorded fixture (CI use)."""
    notice("tester", "replay", "milestone-2.0",
           hint=f"would replay against {fixture}")


if __name__ == "__main__":
    app()
