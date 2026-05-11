"""CLI entrypoint. Subcommands: run, list, show, abort, list-faults, validate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from orchestrator import safety
from orchestrator.loop import Agents, ExperimentRunner
from orchestrator.store import ExperimentStore
from shared.contracts import (
    AbortReason,
    ExperimentPlan,
    ExperimentState,
)

app = typer.Typer(help="Closed-loop chaos engineering orchestrator.", no_args_is_help=True)
console = Console()


# Experiment states that are NOT terminal — abort applies to these.
_LIVE_STATES: set[ExperimentState] = {
    ExperimentState.INITIALIZING,
    ExperimentState.BASELINE,
    ExperimentState.BASELINE_OK,
    ExperimentState.INJECT,
    ExperimentState.INJECTED,
    ExperimentState.VERIFY,
    ExperimentState.REGRESSED,
    ExperimentState.DIAGNOSE,
    ExperimentState.DIAGNOSED,
    ExperimentState.PROPOSE_FIX,
}


def _load_plan(path: Path) -> ExperimentPlan:
    raw = yaml.safe_load(path.read_text())
    return ExperimentPlan.model_validate(raw)


def _store(db: Path | None) -> ExperimentStore:
    return ExperimentStore(db or Path.home() / ".local" / "share" / "chaos" / "experiments.sqlite")


@app.command()
def run(
    plan_path: Path = typer.Argument(..., exists=True, readable=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use mock agents."),
    db: Path | None = typer.Option(None, "--db", help="SQLite path."),
) -> None:
    """Execute one experiment from YAML."""
    plan = _load_plan(plan_path)
    store = _store(db)

    if dry_run:
        from agents._mocks import build_mock_agents

        agents = build_mock_agents()
    else:
        raise typer.BadParameter("real agent wiring lands in milestone 2+ — use --dry-run for now")

    runner = ExperimentRunner(agents=Agents(**agents), store=store)
    record = asyncio.run(runner.run(plan))
    console.print_json(json.dumps(record.model_dump(mode="json")))


@app.command(name="list")
def list_experiments(
    db: Path | None = typer.Option(None, "--db"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List recent experiments."""
    records = _store(db).recent(limit=limit)
    table = Table("experiment_id", "state", "started_at", "abort_reason", "spend_usd")
    for r in records:
        table.add_row(
            r.experiment_id,
            r.state.value,
            r.started_at.isoformat(timespec="seconds"),
            r.abort_reason.value if r.abort_reason else "",
            f"${r.spend_usd:.2f}",
        )
    console.print(table)


@app.command()
def show(experiment_id: str, db: Path | None = typer.Option(None, "--db")) -> None:
    """Show one experiment record as JSON."""
    record = _store(db).load(experiment_id)
    if record is None:
        raise typer.Exit(code=1)
    console.print_json(json.dumps(record.model_dump(mode="json")))


@app.command()
def abort(
    experiment_id: str = typer.Argument(None, help="Experiment ID; omit with --all"),
    all_: bool = typer.Option(False, "--all", help="Abort every non-terminal experiment"),
    reason: AbortReason = typer.Option(AbortReason.USER_KILL, "--reason"),
    detail: str = typer.Option("", "--detail"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """
    Mark an experiment (or all live experiments) as aborted in the store.

    Note: this does NOT clean up Chaos Mesh CRDs in v1 — call `scripts/abort.sh`
    or `kubectl delete <kind>chaos --all` separately. Cluster-side cleanup lands
    with milestone-3.
    """
    if not all_ and not experiment_id:
        raise typer.BadParameter("provide an experiment_id or pass --all")
    if all_ and experiment_id:
        raise typer.BadParameter("--all is mutually exclusive with an experiment_id")

    store = _store(db)
    targets = []
    if all_:
        targets = [r for r in store.recent(limit=200) if r.state in _LIVE_STATES]
    else:
        record = store.load(experiment_id)
        if record is None:
            console.print(f"[red]no experiment {experiment_id} in store[/red]")
            raise typer.Exit(code=1)
        if record.state not in _LIVE_STATES:
            console.print(
                f"[yellow]{experiment_id} already in terminal state {record.state.value}[/yellow]"
            )
            raise typer.Exit(code=0)
        targets = [record]

    if not targets:
        console.print("[green]nothing to abort[/green]")
        return

    for r in targets:
        r.state = ExperimentState.ABORTED
        r.abort_reason = reason
        r.abort_detail = detail or "manual abort"
        store.save(r)
        console.print(f"aborted: {r.experiment_id} (was {r.state.value})")


@app.command(name="list-faults")
def list_faults_cmd(
    category: str | None = typer.Option(None, "--category", help="Filter by category"),
    requires_approval: bool | None = typer.Option(
        None, "--requires-approval/--no-requires-approval"
    ),
) -> None:
    """Print the fault catalogue."""
    from agents.chaos.faults._meta import CATALOGUE

    table = Table("name", "category", "approval", "chaos_mesh_kind", "description")
    for name in sorted(CATALOGUE):
        f = CATALOGUE[name]
        if category and f.category.value != category:
            continue
        if requires_approval is not None and f.requires_approval != requires_approval:
            continue
        table.add_row(
            name,
            f.category.value,
            "yes" if f.requires_approval else "no",
            f.chaos_mesh_kind or "custom",
            f.description,
        )
    console.print(table)


@app.command()
def validate(
    plan_path: Path = typer.Argument(..., exists=True, readable=True),
    skip_safety: bool = typer.Option(
        False, "--skip-safety", help="Don't run cluster denylist / blast-radius checks"
    ),
) -> None:
    """Validate a plan YAML against the schema, the fault catalogue, and safety gates."""
    from agents.chaos.faults._meta import CATALOGUE

    raw = yaml.safe_load(plan_path.read_text())
    try:
        plan = ExperimentPlan.model_validate(raw)
    except Exception as e:
        console.print(f"[red]schema validation failed:[/red] {e}")
        raise typer.Exit(code=1) from e

    # Catalogue check
    missing = [f.name for f in plan.faults if f.name not in CATALOGUE]
    if missing:
        console.print(f"[red]unknown fault(s) in catalogue:[/red] {missing}")
        raise typer.Exit(code=1)

    # Safety checks (deterministic; same as orchestrator pre-flight)
    if not skip_safety:
        if fail := safety.check_cluster_allowed(plan.safety):
            console.print(f"[red]cluster denied:[/red] {fail.detail}")
            raise typer.Exit(code=1)
        if fail := safety.check_blast_radius(plan):
            console.print(f"[red]blast radius:[/red] {fail.detail}")
            raise typer.Exit(code=1)

    console.print(f"[green]ok[/green] — {plan.experiment_id} ({plan.title})")
    console.print(f"  faults: {[f.name for f in plan.faults]}")
    console.print(f"  target: {plan.target_app} in {plan.safety.namespace}@{plan.safety.cluster_context}")


if __name__ == "__main__":
    app()
