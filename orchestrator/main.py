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


# Experiment states that are NOT terminal — abort/pause/resume apply to these.
# Includes transient *_FAIL states (crash between mark + _abort can leave a
# record stuck there) and PAUSED (paused experiments must be reachable by
# subsequent abort / resume CLI calls).
_LIVE_STATES: set[ExperimentState] = {
    ExperimentState.INITIALIZING,
    ExperimentState.BASELINE,
    ExperimentState.BASELINE_OK,
    ExperimentState.BASELINE_FAIL,
    ExperimentState.INJECT,
    ExperimentState.INJECTED,
    ExperimentState.INJECT_FAILED,
    ExperimentState.VERIFY,
    ExperimentState.REGRESSED,
    ExperimentState.DIAGNOSE,
    ExperimentState.DIAGNOSED,
    ExperimentState.PROPOSE_FIX,
    ExperimentState.PAUSED,
}


def _load_plan(path: Path) -> ExperimentPlan:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ExperimentPlan.model_validate(raw)


def _store(db: Path | None) -> ExperimentStore:
    return ExperimentStore(db or Path.home() / ".local" / "share" / "chaos" / "experiments.sqlite")


@app.command()
def run(
    plan_path: Path = typer.Argument(..., exists=True, readable=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use mock agents (no external deps)."),
    profile: str = typer.Option(
        "static",
        "--profile",
        help=(
            "Cognitive strategy mix: static (no LLM, $0), "
            "hybrid (Static + LLM, falls back to Static), or llm (full LLM)."
        ),
    ),
    db: Path | None = typer.Option(None, "--db", help="SQLite path."),
    prom_url: str | None = typer.Option(None, "--prom-url", envvar="PROM_URL"),
    loki_url: str | None = typer.Option(None, "--loki-url", envvar="LOKI_URL"),
    target_repo_path: str | None = typer.Option(
        None, "--target-repo-path", envvar="TARGET_REPO_PATH"
    ),
    kubeconfig: str | None = typer.Option(
        None, "--kubeconfig", envvar="KUBECONFIG",
        help="Path to kubeconfig. Defaults to ~/.kube/config.",
    ),
    kube_context: str | None = typer.Option(
        None, "--kube-context", envvar="KUBE_CONTEXT",
        help="Kubeconfig context to use (e.g. 'kind-chaos-dev').",
    ),
    model: str = typer.Option(
        "claude-opus-4-7",
        "--model",
        envvar="CHAOS_LLM_MODEL",
        help="LLM identifier for hybrid/llm profiles (e.g. 'ollama/qwen2.5-coder:14b').",
    ),
    api_base: str | None = typer.Option(
        None,
        "--api-base",
        envvar="CHAOS_LLM_API_BASE",
        help="Override LLM API base (e.g. 'http://localhost:11434' for Ollama).",
    ),
) -> None:
    """Execute one experiment from YAML.

    Profiles:
      static  - no LLM, deterministic, free. Default.
      hybrid  - Static (always) + LLM (augmenting). Falls back to Static if LLM fails.
      llm     - LLM everywhere. Requires --model + (for non-Anthropic) --api-base.

    --dry-run uses mock agents; ignores profile.
    """
    plan = _load_plan(plan_path)
    store = _store(db)

    from agents._harness import Harness

    harness = Harness()

    if dry_run:
        from agents._mocks import build_mock_agents

        agent_dict = build_mock_agents()
        # Wrap every mock through the harness so dry-run still produces
        # invocation logs (useful for testing the harness itself end-to-end).
        wrapped = {
            name: harness.instrument(name, inst) for name, inst in agent_dict.items()
        }
        agents = Agents(**wrapped)
    else:
        from agents._factory import (
            AgentConfig,
            AgentConfigError,
            Profile,
            build_real_agents,
        )

        if profile not in ("static", "hybrid", "llm"):
            raise typer.BadParameter(
                f"--profile must be one of static / hybrid / llm, got {profile!r}"
            )
        profile_lit: Profile = profile  # type: ignore[assignment]

        cfg = AgentConfig(
            prom_url=prom_url,
            loki_url=loki_url,
            target_repo_path=target_repo_path,
            kubeconfig=kubeconfig,
            kube_context=kube_context,
            model=model,
            api_base=api_base,
        )
        try:
            agents = build_real_agents(cfg, profile=profile_lit, harness=harness)
        except AgentConfigError as e:
            raise typer.BadParameter(str(e)) from e

    runner = ExperimentRunner(agents=agents, store=store, harness=harness)
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
    experiment_id: str | None = typer.Argument(None, help="Experiment ID; omit with --all"),
    all_: bool = typer.Option(False, "--all", help="Abort every non-terminal experiment"),
    reason: AbortReason = typer.Option(AbortReason.USER_KILL, "--reason"),
    detail: str = typer.Option("", "--detail"),
    force: bool = typer.Option(
        False, "--force",
        help=(
            "Directly mark ABORTED in the store without waiting for the "
            "orchestrator to acknowledge. Use only for stale records "
            "(orchestrator already gone)."
        ),
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """
    Request that a running experiment abort.

    Default behavior writes an abort signal to the store; the orchestrator's
    control-poll picks it up at the next state-transition boundary and
    transitions the run to ABORTED gracefully (with cleanup of in-flight
    Chaos Mesh CRDs via the chaos agent's cleanup path).

    With ``--force``, the record is directly marked ABORTED with no signal
    to a running process — use only when you know no orchestrator is alive
    to acknowledge.

    Note: cluster-side cleanup of leftover Chaos Mesh CRDs after a forced
    abort is your responsibility:
        kubectl delete <kind>chaos -l chaos.kosta.dev/experiment-id=<id>
    """
    if not all_ and not experiment_id:
        raise typer.BadParameter("provide an experiment_id or pass --all")
    if all_ and experiment_id:
        raise typer.BadParameter("--all is mutually exclusive with an experiment_id")

    store = _store(db)
    targets = []
    if all_:
        # Don't truncate: a too-small `recent()` limit would silently skip
        # live experiments past the cutoff. Ask the store directly.
        targets = store.find_live(_LIVE_STATES)
    else:
        assert experiment_id is not None  # guarded by typer.BadParameter above
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

    from datetime import UTC, datetime
    now = datetime.now(tz=UTC)
    for r in targets:
        prior_state = r.state.value  # capture BEFORE any mutation
        if force:
            r.state = ExperimentState.ABORTED
            r.abort_reason = reason
            r.abort_detail = detail or "forced abort"
            r.finished_at = now
            store.save(r)
            console.print(f"force-aborted: {r.experiment_id} (was {prior_state})")
        else:
            store.request_abort(r.experiment_id, reason)
            console.print(
                f"abort requested: {r.experiment_id} (currently {prior_state}); "
                "the orchestrator will transition to ABORTED at the next state boundary"
            )


@app.command()
def pause(
    experiment_id: str = typer.Argument(..., help="Experiment ID to pause"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Request a graceful pause at the next state-transition boundary.

    No effect on a terminal experiment (a clear message is printed and the
    command exits 0). The orchestrator's control-poll picks up the flag
    within ``pause_poll_interval_s`` seconds (1s by default).
    """
    store = _store(db)
    record = store.load(experiment_id)
    if record is None:
        console.print(f"[red]no experiment {experiment_id} in store[/red]")
        raise typer.Exit(code=1)
    if record.state not in _LIVE_STATES:
        console.print(
            f"[yellow]{experiment_id} is in terminal state "
            f"{record.state.value}; pause is a no-op[/yellow]"
        )
        return
    if not store.set_pause(experiment_id, True):
        console.print(f"[red]failed to set pause flag on {experiment_id}[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"pause requested: {experiment_id} (currently {record.state.value}); "
        "the orchestrator will pause at the next state boundary"
    )


@app.command()
def resume(
    experiment_id: str = typer.Argument(..., help="Experiment ID to resume"),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Clear the pause flag on a paused experiment.

    The orchestrator's control-poll will see the cleared flag and continue
    to the next state. If the experiment isn't actually paused (no flag set
    and not in PAUSED state), prints a clear message instead of pretending.
    """
    store = _store(db)
    record = store.load(experiment_id)
    if record is None:
        console.print(f"[red]no experiment {experiment_id} in store[/red]")
        raise typer.Exit(code=1)
    ctrl = store.load_control(experiment_id)
    if not ctrl.pause_requested and record.state != ExperimentState.PAUSED:
        console.print(
            f"[yellow]{experiment_id} is not paused "
            f"(state={record.state.value}, pause_requested={ctrl.pause_requested}); "
            "nothing to resume[/yellow]"
        )
        return
    if not store.set_pause(experiment_id, False):
        console.print(f"[red]failed to clear pause flag on {experiment_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"pause cleared: {experiment_id}")


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

    raw = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
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
