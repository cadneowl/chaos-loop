"""Chaos agent. Renders a FaultSpec, applies it, observes lifecycle, and cleans up."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml

from agents._cli import notice
from agents.chaos.cluster import ClusterIO
from agents.chaos.faults import _meta
from agents.chaos.faults._render import RenderContext
from agents.chaos.faults.registry import has_renderer
from agents.chaos.faults.registry import render as render_fault
from shared.contracts import ChaosTimeline, ExperimentPlan, TimelineEvent

log = logging.getLogger(__name__)

# Every Chaos Mesh kind we know how to clean up. Kept in one place so cleanup()
# is consistent with the catalogue.
_CHAOS_MESH_KINDS = (
    "PodChaos",
    "NetworkChaos",
    "IOChaos",
    "StressChaos",
    "DNSChaos",
    "HTTPChaos",
    "TimeChaos",
    "KernelChaos",
)
_CHAOS_MESH_API = "chaos-mesh.org/v1alpha1"
_EXPERIMENT_LABEL = "chaos.kosta.dev/experiment-id"


class ClaudeChaosAgent:
    """Implements `orchestrator.loop.ChaosAgent`.

    Note: the happy path is deterministic CRD application — no LLM needed. The
    Claude-backed surface is reserved for edge cases (status interpretation)
    which we'll route through a small LLM tool only when needed.
    """

    def __init__(
        self,
        *,
        cluster: ClusterIO | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        kubeconfig: str | None = None,
        model: str = "claude-haiku-4-5-20251001",
    ) -> None:
        self._cluster = cluster
        # Sleep is injectable so tests run instantly; default to asyncio.sleep.
        self._sleep: Callable[[float], Awaitable[None]] = sleep_fn or asyncio.sleep
        self.kubeconfig = kubeconfig
        self.model = model

    async def execute(self, plan: ExperimentPlan) -> ChaosTimeline:
        """Render -> apply -> sleep duration -> delete, for each fault in order.

        Quiet windows surround the fault sequence so the tester has clean before/
        after periods. If any step fails, cleanup() is called and the timeline
        is marked unsuccessful.
        """
        if self._cluster is None:
            return ChaosTimeline(
                experiment_id=plan.experiment_id,
                events=[],
                success=False,
                error="no cluster backend configured",
            )

        # Pre-flight: catalogue + renderer + multi-fault gate.
        for fault in plan.faults:
            if fault.name not in _meta.CATALOGUE:
                return self._fail(plan, f"unknown fault {fault.name!r}; not in catalogue")
            if not has_renderer(fault.name):
                return self._fail(plan, f"fault {fault.name!r} has no renderer yet")
        if len(plan.faults) > 1 and not plan.safety.allow_multi_fault:
            return self._fail(plan, "plan has multiple faults but allow_multi_fault is False")

        ctx = RenderContext(
            namespace=plan.safety.namespace, experiment_id=plan.experiment_id
        )
        events: list[TimelineEvent] = []

        try:
            if plan.quiet_window_pre_seconds > 0:
                await self._sleep(plan.quiet_window_pre_seconds)

            for fault in plan.faults:
                events.append(_event(fault.name, "scheduled"))
                body = render_fault(fault, ctx)
                applied = await self._cluster.apply(body)
                events.append(
                    _event(
                        fault.name,
                        "started",
                        f"{applied['kind']}/{applied['metadata']['name']}",
                    )
                )

                await self._sleep(fault.duration_seconds)

                deleted = await self._cluster.delete(
                    body["apiVersion"],
                    body["kind"],
                    body["metadata"]["name"],
                    body["metadata"]["namespace"],
                )
                events.append(
                    _event(
                        fault.name,
                        "cleaned-up",
                        "deleted" if deleted else "already gone",
                    )
                )

            if plan.quiet_window_post_seconds > 0:
                await self._sleep(plan.quiet_window_post_seconds)

            return ChaosTimeline(
                experiment_id=plan.experiment_id, events=events, success=True
            )

        except Exception as e:
            log.warning("chaos execute failed for %s: %r", plan.experiment_id, e)
            events.append(_event("(orchestration)", "error", repr(e)))
            # Best-effort cleanup; never re-raise from the cleanup path.
            try:
                await self.cleanup(plan)
            except Exception as cleanup_err:
                log.warning(
                    "cleanup also failed for %s: %r", plan.experiment_id, cleanup_err
                )
            return ChaosTimeline(
                experiment_id=plan.experiment_id,
                events=events,
                success=False,
                error=repr(e),
            )

    async def get_namespace_annotations(self, namespace: str) -> dict[str, str] | None:
        """Delegate to the cluster backend. Returns None when no backend is wired."""
        if self._cluster is None:
            return None
        return await self._cluster.get_namespace_annotations(namespace)

    async def cleanup(self, plan: ExperimentPlan) -> None:
        """Delete every Chaos Mesh resource labeled with this experiment_id.

        Best-effort: per-kind errors are swallowed so a single missing CRD type
        doesn't abort the whole sweep.
        """
        if self._cluster is None:
            return
        label_selector = {_EXPERIMENT_LABEL: plan.experiment_id}
        for kind in _CHAOS_MESH_KINDS:
            try:
                resources = await self._cluster.list_by_labels(
                    _CHAOS_MESH_API, kind, plan.safety.namespace, label_selector
                )
            except Exception as e:
                log.debug("list %s in %s failed: %r", kind, plan.safety.namespace, e)
                continue
            for r in resources:
                try:
                    await self._cluster.delete(
                        r["apiVersion"],
                        r["kind"],
                        r["metadata"]["name"],
                        r["metadata"]["namespace"],
                    )
                except Exception as e:
                    log.debug("delete %s/%s failed: %r", kind, r["metadata"]["name"], e)

    def _fail(self, plan: ExperimentPlan, msg: str) -> ChaosTimeline:
        return ChaosTimeline(
            experiment_id=plan.experiment_id,
            events=[_event("(preflight)", "error", msg)],
            success=False,
            error=msg,
        )


def _event(fault_name: str, event: str, detail: str = "") -> TimelineEvent:
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
    """Render one FaultSpec to its Chaos Mesh CRD YAML on stdout. No apply."""
    raw = yaml.safe_load(plan_path.read_text())
    plan = ExperimentPlan.model_validate(raw)
    if index >= len(plan.faults):
        typer.echo(f"index {index} out of range; plan has {len(plan.faults)} fault(s)", err=True)
        raise typer.Exit(2)
    fault = plan.faults[index]
    if fault.name not in _meta.CATALOGUE:
        typer.echo(f"unknown fault: {fault.name}", err=True)
        raise typer.Exit(1)
    if not has_renderer(fault.name):
        kind = _meta.CATALOGUE[fault.name].chaos_mesh_kind or "custom"
        notice("chaos", f"render({fault.name})", "milestone-3.x",
               hint=f"in catalogue as {kind}; renderer not yet implemented")
        return
    ctx = RenderContext(namespace=plan.safety.namespace, experiment_id=plan.experiment_id)
    crd = render_fault(fault, ctx)
    typer.echo(yaml.safe_dump(crd, sort_keys=False))


if __name__ == "__main__":
    app()
