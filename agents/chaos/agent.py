"""Chaos agent. Renders a FaultSpec, applies it, observes lifecycle, and cleans up."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from agents._cli import notice
from agents.chaos.faults import _meta
from shared.contracts import ChaosTimeline, ExperimentPlan, TimelineEvent


class ClaudeChaosAgent:
    """Implements `orchestrator.loop.ChaosAgent`.

    Note: this agent does NOT need an LLM for the happy path — fault execution is
    deterministic CRD application. The Claude-backed surface is reserved for
    edge cases (e.g., "the CRD didn't reach Active, what should we do?"), which
    we route through a small LLM tool only when needed.
    """

    def __init__(self, *, kubeconfig: str | None = None, model: str = "claude-haiku-4-5-20251001") -> None:
        self.kubeconfig = kubeconfig
        self.model = model

    async def execute(self, plan: ExperimentPlan) -> ChaosTimeline:
        # Validate every fault has a registered renderer
        for fault in plan.faults:
            if fault.name not in _meta.CATALOGUE:
                return ChaosTimeline(
                    experiment_id=plan.experiment_id,
                    events=[],
                    success=False,
                    error=f"unknown fault {fault.name!r}; not in catalogue",
                )

        # TODO(milestone-3): real Chaos Mesh apply/observe/cleanup loop via kubernetes client.
        # For each fault:
        #   1. event "scheduled"
        #   2. render CRD (catalogue entry's render fn)
        #   3. apply
        #   4. poll until Active -> event "started"
        #   5. wait duration_seconds
        #   6. delete CRD -> event "stopped" -> event "cleaned-up"
        raise NotImplementedError(
            "ClaudeChaosAgent.execute is a milestone-3 task; use --dry-run for now"
        )

    async def cleanup(self, plan: ExperimentPlan) -> None:
        # TODO(milestone-3): delete all CRDs created for this experiment, including orphans
        raise NotImplementedError("milestone-3")


# Helper used by future implementation
def _now_event(fault_name: str, event: str, detail: str = "") -> TimelineEvent:
    return TimelineEvent(
        timestamp=datetime.now(tz=UTC),
        fault_name=fault_name,
        event=event,  # type: ignore[arg-type]
        detail=detail,
    )


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(help="Chaos agent — render, inject, cleanup.", no_args_is_help=True)


@app.command()
def render(
    plan_path: Path = typer.Argument(..., exists=True, readable=True),
    index: int = typer.Option(0, "--index", help="fault index in the plan"),
) -> None:
    """Render one FaultSpec to YAML on stdout. No apply."""
    raw = yaml.safe_load(plan_path.read_text())
    plan = ExperimentPlan.model_validate(raw)
    if index >= len(plan.faults):
        typer.echo(f"index {index} out of range; plan has {len(plan.faults)} fault(s)", err=True)
        raise typer.Exit(2)
    fault = plan.faults[index]
    if fault.name not in _meta.CATALOGUE:
        typer.echo(f"unknown fault: {fault.name}", err=True)
        raise typer.Exit(1)
    kind = _meta.CATALOGUE[fault.name].chaos_mesh_kind or "custom"
    notice("chaos", "render", "milestone-3.0",
           hint=f"would render {fault.name!r} as {kind} CRD")


@app.command()
def inject(plan_path: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Apply one plan's faults out-of-band of the orchestrator. Debug only."""
    notice("chaos", "inject", "milestone-3.0",
           hint="bypasses orchestrator safety gates — only use against --dry-run targets")


@app.command()
def cleanup(namespace: str = typer.Option("otel-demo", "--namespace")) -> None:
    """Delete every Chaos Mesh CRD in a namespace."""
    notice("chaos", "cleanup", "milestone-3.4",
           hint=f"would clean Chaos Mesh CRDs in namespace={namespace!r}")


if __name__ == "__main__":
    app()
