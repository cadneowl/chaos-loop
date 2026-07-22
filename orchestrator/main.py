"""CLI entrypoint. Subcommands: run, list, show, abort, list-faults, validate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from orchestrator import safety
from orchestrator.loop import Agents, ExperimentRunner
from orchestrator.store import ExperimentStore
from shared.contracts import (
    AbortReason,
    CoverageMatrix,
    ExperimentPlan,
    ExperimentState,
    SuiteRunRecord,
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
    hardware: bool = typer.Option(
        False,
        "--hardware",
        help=(
            "Target a hardware bench instead of Kubernetes. Wires "
            "HardwareChaosAgent + HardwareTelemetryBackend against an "
            "in-process simulator (Phase 1) or a real bench via "
            "HilHardwareIO (Phase 2; not wired here yet)."
        ),
    ),
    plugin: str | None = typer.Option(
        None,
        "--plugin",
        help=(
            "Experiment plugin owning the env/test lifecycle (provision, seed, "
            "setup, custom verify, teardown). Overrides plan.plugin. "
            "See `chaos plugins list`."
        ),
    ),
) -> None:
    """Execute one experiment from YAML.

    Profiles:
      static  - no LLM, deterministic, free. Default.
      hybrid  - Static (always) + LLM (augmenting). Falls back to Static if LLM fails.
      llm     - LLM everywhere. Requires --model + (for non-Anthropic) --api-base.

    --dry-run uses mock agents; ignores profile.
    --hardware swaps the chaos + tester backends to talk to a hardware bench.
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
    elif hardware:
        # Hardware bench wiring — Phase 1 uses SimulatedHardwareIO so the
        # CLI works without a real ESP32 plugged in. Phase 2 will switch
        # to `HilHardwareIO` once the attack-ESP32 firmware lands.
        from agents._mocks import build_mock_agents
        from agents.chaos.hardware_agent import HardwareChaosAgent
        from agents.chaos.hardware_io import SimulatedHardwareIO
        from agents.tester.agent import ClaudeTesterAgent
        from agents.tester.tools.hardware_telemetry import HardwareTelemetryBackend

        sim = SimulatedHardwareIO()
        mocks = build_mock_agents()  # security / diagnostician / fixer stay mocked
        wrapped = {
            "tester": harness.instrument(
                "tester",
                ClaudeTesterAgent(prom_backend=HardwareTelemetryBackend(sim)),
            ),
            "security": harness.instrument("security", mocks["security"]),
            "chaos": harness.instrument("chaos", HardwareChaosAgent(hardware=sim)),
            "diagnostician": harness.instrument("diagnostician", mocks["diagnostician"]),
            "fixer": harness.instrument("fixer", mocks["fixer"]),
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

    # Resolve the plugin (CLI flag overrides plan.plugin). A null plugin leaves
    # the run unchanged from its pre-plugin behavior.
    plugin_name = plugin or plan.plugin
    plugin_obj = None
    if plugin_name:
        from plugins.registry import PluginError, load_plugin

        try:
            plugin_obj = load_plugin(plugin_name)
        except PluginError as e:
            raise typer.BadParameter(str(e)) from e

    runner = ExperimentRunner(
        agents=agents, store=store, harness=harness, plugin=plugin_obj
    )
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


suppress_app = typer.Typer(
    help="Manage .chaos/suppress.yaml hypothesis suppression rules.",
    no_args_is_help=True,
)
app.add_typer(suppress_app, name="suppress")


@suppress_app.command("list")
def suppress_list_cmd() -> None:
    """List active suppression rules from .chaos/suppress.yaml (cwd-relative)."""
    from orchestrator.suppression import load_repo_suppress_list

    rules = load_repo_suppress_list().rules
    if not rules:
        console.print(
            "[yellow]no suppression rules — .chaos/suppress.yaml is empty or missing[/yellow]"
        )
        return

    table = Table("match", "reason", "expires_at")
    for r in rules:
        parts = []
        for label in ("hypothesis_id", "fix_class", "path_glob", "summary_contains"):
            val = getattr(r, label)
            if val is not None:
                parts.append(f"{label}={val!r}")
        match_str = " AND ".join(parts) if parts else "—"
        table.add_row(
            match_str,
            r.reason or "[dim]—[/dim]",
            r.expires_at.isoformat() if r.expires_at else "[dim]—[/dim]",
        )
    console.print(table)


@suppress_app.command("add")
def suppress_add_cmd(
    experiment_id: str = typer.Argument(..., help="Experiment ID containing the hypothesis"),
    hypothesis_index: int = typer.Argument(
        ..., min=1, help="1-based index of the hypothesis to suppress"
    ),
    reason: str = typer.Option(
        "", "--reason", "-r", help="Free-text reason kept in the audit trail"
    ),
    expires: str | None = typer.Option(
        None, "--expires", help="ISO-8601 datetime when this rule stops matching"
    ),
    db: Path | None = typer.Option(None, "--db"),
) -> None:
    """Suppress one recorded hypothesis from triggering the fixer.

    Looks up the hypothesis by its 1-based index in the experiment's
    diagnosis, then appends a rule keyed by the hypothesis's stable
    fingerprint to ``<cwd>/.chaos/suppress.yaml`` (creating the file if
    needed). On the next run, the orchestrator skips ``propose_fix`` for
    any hypothesis with the same fingerprint.
    """
    from datetime import datetime as _dt

    from orchestrator.suppression import SuppressList
    from shared.contracts import SuppressionRule

    store = _store(db)
    record = store.load(experiment_id)
    if record is None:
        console.print(f"[red]no experiment {experiment_id} in store[/red]")
        raise typer.Exit(code=1)
    if record.diagnosis is None or not record.diagnosis.hypotheses:
        console.print(f"[red]{experiment_id} has no diagnosis to suppress[/red]")
        raise typer.Exit(code=1)
    if hypothesis_index > len(record.diagnosis.hypotheses):
        console.print(
            f"[red]hypothesis_index {hypothesis_index} out of range; "
            f"the experiment has {len(record.diagnosis.hypotheses)} hypotheses[/red]"
        )
        raise typer.Exit(code=1)

    h = record.diagnosis.hypotheses[hypothesis_index - 1]

    parsed_expires = None
    if expires is not None:
        try:
            parsed_expires = _dt.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError as e:
            console.print(f"[red]invalid --expires value:[/red] {e}")
            raise typer.Exit(code=1) from e

    new_rule = SuppressionRule(
        hypothesis_id=h.id,
        reason=reason or f"suppressed via `chaos suppress add` against {experiment_id}",
        expires_at=parsed_expires,
    )

    # Load existing or start fresh; append; serialize.
    suppress_path = Path.cwd() / ".chaos" / "suppress.yaml"
    if suppress_path.exists():
        existing_raw = yaml.safe_load(suppress_path.read_text(encoding="utf-8")) or {}
        existing = SuppressList.model_validate(existing_raw)
        existing.rules.append(new_rule)
    else:
        suppress_path.parent.mkdir(parents=True, exist_ok=True)
        existing = SuppressList(rules=[new_rule])

    # `mode="json"` so datetimes become strings (yaml.safe_dump can't serialize
    # datetimes natively); `exclude_none=True` keeps the file readable by
    # omitting unset optional fields.
    serialized = existing.model_dump(mode="json", exclude_none=True)
    suppress_path.write_text(
        yaml.safe_dump(serialized, sort_keys=False), encoding="utf-8"
    )

    console.print(f"[green]added suppression rule for hypothesis {h.id}[/green]")
    console.print(f"  summary    : {h.summary}")
    console.print(f"  fix_class  : {h.suggested_fix_class}")
    console.print(f"  written to : {suppress_path}")


plugins_app = typer.Typer(
    help="Inspect experiment plugins (env/test lifecycle hooks).",
    no_args_is_help=True,
)
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list_cmd() -> None:
    """List discovered experiment plugins (entry points + local dir)."""
    from plugins.base import _HOOK_NAMES, class_overrides
    from plugins.registry import PLUGINS, discover_plugins

    discover_plugins()
    if not PLUGINS:
        console.print(
            "[yellow]no plugins discovered. Register via the 'chaos.plugins' "
            "entry-point group or drop a module in $CHAOS_PLUGINS_DIR "
            "(default ./chaos_plugins).[/yellow]"
        )
        return
    table = Table("name", "class", "hooks implemented")
    for name in sorted(PLUGINS):
        cls = PLUGINS[name]
        # Class-level introspection — never construct the plugin just to list it.
        hooks = [h for h in _HOOK_NAMES if class_overrides(cls, h)]
        table.add_row(name, cls.__name__, ", ".join(hooks) or "[dim]none[/dim]")
    console.print(table)


catalogue_app = typer.Typer(
    help="Inspect / validate the fault catalogue end-to-end against the simulator.",
    no_args_is_help=True,
)
app.add_typer(catalogue_app, name="catalogue")


@catalogue_app.command("verify")
def catalogue_verify_cmd(
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated fault names to verify (default: every hardware fault).",
    ),
) -> None:
    """Render + inject + clean up every hardware fault against SimulatedHardwareIO.

    Used in CI to catch catalogue / renderer / simulator drift before a
    bench operator does. Exits non-zero if any fault fails its loop.
    """
    from agents.chaos.faults._meta import CATALOGUE
    from agents.chaos.hardware_agent import HardwareChaosAgent
    from agents.chaos.hardware_io import SimulatedHardwareIO
    from shared.contracts import FaultSpec, SafetyConstraints

    # Hardware faults = catalogue entries with chaos_mesh_kind None AND a
    # hardware category. (Some non-CRD entries like `secret.rotate` use
    # other agents; not our concern here.)
    hardware_categories = {"rf", "power", "sensor", "time"}
    if only:
        wanted = {n.strip() for n in only.split(",") if n.strip()}
        candidates = [n for n in wanted if n in CATALOGUE]
        missing = wanted - set(candidates)
        if missing:
            console.print(f"[red]unknown fault names:[/red] {sorted(missing)}")
            raise typer.Exit(code=1)
    else:
        candidates = sorted(
            name
            for name, defn in CATALOGUE.items()
            if defn.chaos_mesh_kind is None and defn.category.value in hardware_categories
        )

    table = Table("fault", "category", "result", "detail")
    failures: list[str] = []

    async def _no_sleep(_s: float) -> None:
        return None

    async def _verify_one(name: str) -> tuple[bool, str]:
        defn = CATALOGUE[name]
        sim = SimulatedHardwareIO()
        agent = HardwareChaosAgent(hardware=sim, sleep_fn=_no_sleep)
        plan = ExperimentPlan(
            title=f"catalogue-verify::{name}",
            target_app="neoowl-sim",
            faults=[
                FaultSpec(
                    category=defn.category,
                    name=name,
                    target_selector={"device": "dut-1"},
                    parameters={},
                    duration_seconds=2,
                    requires_approval=False,
                    rationale=f"catalogue verify for {name}",
                )
            ],
            safety=SafetyConstraints(
                cluster_context="bench-hardware",
                namespace="bench",
                require_namespace_annotation=False,
            ),
        )
        timeline = await agent.execute(plan)
        if not timeline.success:
            return False, timeline.error or "unknown failure"
        # Sanity: every fault should leave the active-faults dict empty.
        if sim._active_faults:
            return False, f"leak: {len(sim._active_faults)} active fault(s) after cleanup"
        return True, "ok"

    async def _run_all() -> None:
        for name in candidates:
            passed, detail = await _verify_one(name)
            table.add_row(
                name,
                CATALOGUE[name].category.value,
                "[green]pass[/green]" if passed else "[red]fail[/red]",
                detail,
            )
            if not passed:
                failures.append(f"{name}: {detail}")

    asyncio.run(_run_all())
    console.print(table)
    if failures:
        console.print(f"[red]{len(failures)} fault(s) failed verification[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[green]ok[/green] — {len(candidates)} hardware faults verified end-to-end"
    )


regression_app = typer.Typer(
    help="Resilience regression suites: replay frozen scenarios, report coverage.",
    no_args_is_help=True,
)
app.add_typer(regression_app, name="regression")


def _build_regression_agents(
    *,
    dry_run: bool,
    profile: str,
    harness: Any,
    prom_url: str | None,
    loki_url: str | None,
    target_repo_path: str | None,
    kubeconfig: str | None,
    kube_context: str | None,
    model: str,
    api_base: str | None,
) -> Agents:
    """Build the agent set for a regression run (dry-run mocks or real agents).

    Mirrors the non-hardware wiring of ``run``; hardware benches are out of
    scope for the v1 regression suite.
    """
    if dry_run:
        from agents._mocks import build_mock_agents

        wrapped = {
            name: harness.instrument(name, inst)
            for name, inst in build_mock_agents().items()
        }
        return Agents(**wrapped)

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
        return build_real_agents(cfg, profile=profile_lit, harness=harness)
    except AgentConfigError as e:
        raise typer.BadParameter(str(e)) from e


def _print_suite_run(record: SuiteRunRecord) -> None:
    from shared.contracts import RegressionOutcome

    styles = {
        RegressionOutcome.PASS: "green",
        RegressionOutcome.REGRESSED: "red",
        RegressionOutcome.BASELINE_FAIL: "yellow",
        RegressionOutcome.ERROR: "magenta",
    }
    table = Table("scenario", "fault", "outcome", "detail")
    for v in record.verdicts:
        style = styles.get(v.outcome, "white")
        if v.outcome == RegressionOutcome.REGRESSED:
            detail = "newly failing: " + (", ".join(v.newly_failing) or "?")
        elif v.outcome in (RegressionOutcome.ERROR, RegressionOutcome.BASELINE_FAIL):
            detail = v.detail or "[dim]—[/dim]"
        else:
            detail = "[dim]—[/dim]"
        table.add_row(
            v.title or v.scenario_id,
            v.fault or "[dim]?[/dim]",
            f"[{style}]{v.outcome.value}[/{style}]",
            detail,
        )
    console.print(table)
    counts = {o: 0 for o in RegressionOutcome}
    for v in record.verdicts:
        counts[v.outcome] += 1
    console.print(
        f"suite_run={record.suite_run_id}  scenarios={len(record.verdicts)}  "
        f"[green]pass={counts[RegressionOutcome.PASS]}[/green]  "
        f"[red]regressed={counts[RegressionOutcome.REGRESSED]}[/red]  "
        f"[yellow]baseline_fail={counts[RegressionOutcome.BASELINE_FAIL]}[/yellow]  "
        f"[magenta]error={counts[RegressionOutcome.ERROR]}[/magenta]"
    )
    console.print(f"[dim]inspect: chaos regression show {record.suite_run_id}[/dim]")
    if record.coverage is not None:
        _print_coverage_summary(record.coverage)


def _print_coverage_summary(matrix: CoverageMatrix) -> None:
    comp = (
        "n/a"
        if matrix.comprehensiveness is None
        else f"{matrix.comprehensiveness:.0%}"
    )
    console.print(
        f"coverage: [green]{matrix.covered} covered[/green] / "
        f"[yellow]{matrix.gaps} gap[/yellow] / {matrix.na} n-a  "
        f"(relevant comprehensiveness {comp})"
    )
    console.print(
        f"[dim]axis: {len(matrix.faults)} fault(s) x {len(matrix.journeys)} journey(s). "
        f"Scope with --fault; add scenarios to close gaps.[/dim]"
    )


@regression_app.command("run")
def regression_run_cmd(
    suite_path: Path = typer.Argument(..., exists=True, readable=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use mock agents (no external deps)."),
    profile: str = typer.Option("static", "--profile", help="static / hybrid / llm."),
    db: Path | None = typer.Option(None, "--db", help="SQLite path."),
    prom_url: str | None = typer.Option(None, "--prom-url", envvar="PROM_URL"),
    loki_url: str | None = typer.Option(None, "--loki-url", envvar="LOKI_URL"),
    target_repo_path: str | None = typer.Option(
        None, "--target-repo-path", envvar="TARGET_REPO_PATH"
    ),
    kubeconfig: str | None = typer.Option(None, "--kubeconfig", envvar="KUBECONFIG"),
    kube_context: str | None = typer.Option(None, "--kube-context", envvar="KUBE_CONTEXT"),
    model: str = typer.Option("claude-opus-4-7", "--model", envvar="CHAOS_LLM_MODEL"),
    api_base: str | None = typer.Option(None, "--api-base", envvar="CHAOS_LLM_API_BASE"),
) -> None:
    """Replay every scenario in a regression suite and report verdicts + coverage."""
    from regression.scenario import load_suite
    from regression.suite_runner import SuiteRunner

    suite = load_suite(suite_path)
    store = _store(db)

    from agents._harness import Harness

    harness = Harness()
    agents = _build_regression_agents(
        dry_run=dry_run,
        profile=profile,
        harness=harness,
        prom_url=prom_url,
        loki_url=loki_url,
        target_repo_path=target_repo_path,
        kubeconfig=kubeconfig,
        kube_context=kube_context,
        model=model,
        api_base=api_base,
    )
    suite_runner = SuiteRunner.with_agents(agents, store, harness=harness)
    overrides: dict[str, object] = {"_dry_run": True} if dry_run else {}

    from shared.contracts import RegressionOutcome, RegressionVerdict

    _dots = {
        RegressionOutcome.PASS: "[green]pass[/green]",
        RegressionOutcome.REGRESSED: "[red]REGRESSED[/red]",
        RegressionOutcome.BASELINE_FAIL: "[yellow]baseline_fail[/yellow]",
        RegressionOutcome.ERROR: "[magenta]error[/magenta]",
    }

    def _progress(done: int, total: int, v: RegressionVerdict) -> None:
        console.print(
            f"[dim][{done}/{total}][/dim] {v.title or v.scenario_id} "
            f"… {_dots.get(v.outcome, v.outcome.value)}"
        )

    record = asyncio.run(
        suite_runner.run(
            suite, plugin_config_overrides=overrides, on_progress=_progress
        )
    )
    console.print()
    _print_suite_run(record)
    if any(v.outcome.value == "regressed" for v in record.verdicts):
        raise typer.Exit(code=1)


@regression_app.command("coverage")
def regression_coverage_cmd(
    suite_path: Path = typer.Argument(..., exists=True, readable=True),
    fault: list[str] = typer.Option(
        None,
        "--fault",
        help="Scope the fault axis (repeatable). Default: catalogue faults in the "
        "categories the suite uses.",
    ),
) -> None:
    """Render the fault-by-journey coverage matrix for a suite (no runs required)."""
    from regression.coverage import CoverageReporter
    from regression.scenario import load_suite

    suite = load_suite(suite_path)
    selected = list(fault or [])
    matrix = CoverageReporter().render(suite, faults=selected)
    _print_coverage_summary(matrix)
    # Detailed grid only when the caller scoped the axis (else it's unreadably wide).
    if selected:
        by_key = {(c.fault, c.journey): c for c in matrix.cells}
        table = Table("journey", *matrix.faults)
        for journey in matrix.journeys:
            marks = [
                "[green]✓[/green]"
                if (cell := by_key.get((f, journey))) and cell.scenario_id
                else "·"
                for f in matrix.faults
            ]
            table.add_row(journey, *marks)
        console.print(table)


@regression_app.command("validate")
def regression_validate_cmd(
    suite_path: Path = typer.Argument(..., exists=True, readable=True),
) -> None:
    """Check a suite for problems (bad fault names, journeys not in all_journeys).

    Runs nothing — a pure offline lint of the suite file.
    """
    from regression.scenario import load_suite, validate_suite

    suite = load_suite(suite_path, validate=False)
    problems = validate_suite(suite)
    if problems:
        console.print(f"[red]{len(problems)} problem(s) in {suite_path.name}:[/red]")
        for p in problems:
            console.print(f"  [red]•[/red] {p}")
        raise typer.Exit(code=1)
    console.print(
        f"[green]ok[/green] — {len(suite.scenarios)} scenario(s), "
        f"{len(suite.all_journeys)} journey(s), no problems."
    )


@regression_app.command("list")
def regression_list_cmd(
    db: Path | None = typer.Option(None, "--db"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """List recent regression suite runs."""
    from shared.contracts import RegressionOutcome

    runs = _store(db).recent_suite_runs(limit)
    if not runs:
        console.print("[dim]no suite runs recorded yet.[/dim]")
        return
    table = Table("suite_run", "suite", "started", "scenarios", "regressed")
    for r in runs:
        regressed = sum(
            1 for v in r.verdicts if v.outcome == RegressionOutcome.REGRESSED
        )
        table.add_row(
            r.suite_run_id,
            r.suite_id,
            r.started_at.isoformat(timespec="seconds"),
            str(len(r.verdicts)),
            f"[red]{regressed}[/red]" if regressed else "0",
        )
    console.print(table)


def _scaffold_yaml(
    name: str, target_app: str, suite_path: str, journeys: list[str]
) -> str:
    """A minimal, commented starter suite — only the fields a human needs to edit."""
    all_lines = "\n".join(f"  - {json.dumps(j)}" for j in journeys)
    return f"""\
# Regression suite for {target_app}, generated by `chaos regression scaffold`.
# Edit the scenario below: pick a real fault (see `chaos list-faults`), say why
# it threatens the journey, and list the journey(s) it must not break.
name: {name}
target_app: {target_app}
# target_repo: https://git/...        # optional; enables source-aware diagnosis

safety:
  cluster_context: kind-chaos          # a NON-prod cluster
  namespace: {target_app}
  # On a shared cluster set this true and annotate the namespace; left false so
  # a local `kind` run works out of the box.
  require_namespace_annotation: false

oracle: playwright
oracle_defaults:
  suite_path: {json.dumps(suite_path)}   # your Playwright project directory
  # base_url: http://localhost:8080
  retries: 2

scenarios:
  - title: "{target_app} survives a pod restart"
    fault:
      category: pod
      name: pod.kill                   # see `chaos list-faults`
      target_selector: {{ app: {target_app} }}
      duration_seconds: 30
      rationale: "TODO: why this fault threatens the journey below"
    journeys:                          # which journey(s) this fault must not break
      - {json.dumps(journeys[0])}

# Every journey your suite exposes — the coverage denominator:
all_journeys:
{all_lines}
"""


@regression_app.command("scaffold")
def regression_scaffold_cmd(
    out_path: Path = typer.Argument(..., help="Where to write the starter suite YAML."),
    suite_path: str = typer.Option(".", "--suite-path", help="Playwright project dir."),
    list_json: Path | None = typer.Option(
        None,
        "--list-json",
        help="A `playwright test --list --reporter=json` dump. If omitted, runs npx.",
    ),
    target_app: str = typer.Option("my-app", "--target-app"),
) -> None:
    """Generate a starter suite by enumerating a Playwright project's journeys."""
    import subprocess

    from regression.oracles.playwright import list_journeys
    from regression.scenario import load_suite

    if list_json is not None:
        data = json.loads(list_json.read_text(encoding="utf-8"))
    else:
        proc = subprocess.run(
            ["npx", "playwright", "test", "--list", "--reporter=json"],
            cwd=suite_path,
            capture_output=True,
            text=True,
        )
        if not proc.stdout.strip():
            console.print(
                f"[red]could not list Playwright tests in {suite_path!r}[/red]\n"
                f"{proc.stderr[:500]}"
            )
            raise typer.Exit(code=1)
        data = json.loads(proc.stdout)

    journeys = list_journeys(data)
    if not journeys:
        console.print("[yellow]no journeys found — is --suite-path correct?[/yellow]")
        raise typer.Exit(code=1)

    out_path.write_text(
        _scaffold_yaml(f"{target_app}-resilience", target_app, suite_path, journeys),
        encoding="utf-8",
    )
    # Round-trip so we never hand back a file that won't load.
    load_suite(out_path)
    console.print(
        f"[green]wrote[/green] {out_path} — {len(journeys)} journey(s), 1 example scenario.\n"
        "Before running:\n"
        "  1. edit the scenario's fault + rationale, and the journey(s) it guards\n"
        "  2. ensure Node + Playwright are installed and the target is reachable\n"
        f"  3. chaos regression validate {out_path}\n"
        f"  4. chaos regression run {out_path}"
    )


@regression_app.command("show")
def regression_show_cmd(
    suite_run_id: str, db: Path | None = typer.Option(None, "--db")
) -> None:
    """Show a stored suite run by id."""
    record = _store(db).load_suite_run(suite_run_id)
    if record is None:
        console.print(f"[red]no suite run found for {suite_run_id!r}[/red]")
        raise typer.Exit(code=1)
    _print_suite_run(record)


if __name__ == "__main__":
    app()
