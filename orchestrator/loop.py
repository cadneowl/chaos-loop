"""
The orchestration state machine.

Deterministic Python. Calls into agent adapters for the cognitive work; owns all
state transitions, safety gates, and persistence.

In v0 (this commit), agent adapters are protocols and the loop body has
``raise NotImplementedError`` markers where real agent invocations will plug in.
Once `agents/*/agent.py` exposes concrete `invoke(...)` callables matching the
Protocol, the loop runs end-to-end without changes here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from orchestrator import safety, suppression
from orchestrator.budget import BudgetTracker
from orchestrator.store import ExperimentStore
from plugins.base import ExperimentPlugin
from plugins.host import GuardTripped, Session, open_session
from shared.contracts import (
    AbortReason,
    ChaosTimeline,
    DiagnosisReport,
    DiagnosisRequest,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentState,
    FixProposal,
    SecurityReport,
    SecurityRequest,
    TesterReport,
    TesterRequest,
)

log = logging.getLogger(__name__)


# ---------- agent adapter protocols --------------------------------------------


class TesterAgent(Protocol):
    async def baseline(self, req: TesterRequest) -> TesterReport: ...
    async def verify(self, req: TesterRequest) -> TesterReport: ...


class SecurityAgent(Protocol):
    async def baseline(self, req: SecurityRequest) -> SecurityReport: ...
    async def verify(self, req: SecurityRequest) -> SecurityReport: ...


class ChaosAgent(Protocol):
    async def execute(self, plan: ExperimentPlan) -> ChaosTimeline: ...
    async def cleanup(self, plan: ExperimentPlan) -> None: ...
    # Optional: gate uses this to verify namespace annotation. Implementations
    # without a cluster reference (e.g., mocks) can omit it; the loop treats
    # AttributeError as "annotations unavailable".
    async def get_namespace_annotations(self, namespace: str) -> dict[str, str] | None: ...


class DiagnosticianAgent(Protocol):
    async def diagnose(self, req: DiagnosisRequest) -> DiagnosisReport: ...


class FixerAgent(Protocol):
    async def propose_fix(self, diagnosis: DiagnosisReport) -> FixProposal: ...


@dataclass
class Agents:
    tester: TesterAgent
    security: SecurityAgent
    chaos: ChaosAgent
    diagnostician: DiagnosticianAgent
    fixer: FixerAgent


# ---------- loop ---------------------------------------------------------------


class ExperimentRunner:
    def __init__(
        self,
        agents: Agents,
        store: ExperimentStore,
        *,
        harness: Any | None = None,  # agents._harness.Harness; typed loosely to avoid the import cycle
        plugin: ExperimentPlugin | None = None,
        pause_poll_interval_s: float = 1.0,
    ) -> None:
        self.agents = agents
        self.store = store
        self.harness = harness
        self.plugin = plugin
        # The active plugin session, set for the duration of run(). None outside
        # a run or when no plugin is configured.
        self._session: Session | None = None
        # How often we re-check the control flags while paused. Exposed for
        # tests; production keeps the default 1s.
        self.pause_poll_interval_s = pause_poll_interval_s

    async def run(self, plan: ExperimentPlan) -> ExperimentRecord:
        """Run one experiment.

        The body runs inside a plugin session (a no-op when no plugin is
        configured) so the plugin's env/test teardown hooks are guaranteed to
        run on every exit path — success, abort, or crash. The cheap, plan-only
        safety gates run *before* the session opens so an unsafe plan never
        provisions an environment.
        """
        record = ExperimentRecord(
            experiment_id=plan.experiment_id,
            plan=plan,
            state=ExperimentState.INITIALIZING,
        )
        self._attach_invocations(record)
        self.store.save(record)

        # --- pre-flight safety gates that need no environment ---------------
        # Run before provisioning: reject an unsafe plan without standing up
        # anything. The namespace-annotation gate moves *inside* the session
        # since provisioning may be what creates/annotates the namespace.
        if fail := safety.check_cluster_allowed(plan.safety):
            return self._abort(record, fail.reason, fail.detail)
        if fail := safety.check_blast_radius(plan):
            return self._abort(record, fail.reason, fail.detail)

        session = open_session(plan, self.plugin)
        self._session = session
        try:
            async with session:
                record = await self._run_body(plan, record, session)
        except Exception as e:
            # A plugin setup/teardown hook (or an unexpected agent error) raised.
            # An exception can only escape _run_body before any terminal
            # transition (the _abort/_finish paths return, never raise), so the
            # record is non-terminal here. Teardown has already run via the
            # session's unwind; record this as a graceful ABORTED rather than
            # crashing the caller with a traceback.
            record = self._abort(
                record, AbortReason.AGENT_FAILURE, f"lifecycle failed: {e!r}"
            )
        finally:
            # Re-sync after teardown so the persisted record carries the full
            # plugin audit trail (including teardown stage results) and save it.
            self._sync_plugin(record)
            self.store.save(record)
            self._session = None
        return record

    async def _run_body(
        self, plan: ExperimentPlan, record: ExperimentRecord, session: Session
    ) -> ExperimentRecord:
        budget = BudgetTracker(plan.budget)

        # Plugin setup (provision_env / seed / setup_test) has already run in
        # session.__aenter__. Persist its stage results before going further.
        self._sync_plugin(record)
        self.store.save(record)

        if plan.safety.require_namespace_annotation:
            annotations = await self._fetch_namespace_annotations(plan.safety.namespace)
            if fail := safety.check_namespace_annotation(plan.safety, annotations):
                return self._abort(record, fail.reason, fail.detail)

        # --- baseline -------------------------------------------------------
        if fail := await self._check_control(record):
            return self._abort(record, fail.reason, fail.detail)
        record.state = ExperimentState.BASELINE
        self.store.save(record)

        record.tester_baseline = await self.agents.tester.baseline(
            TesterRequest(
                kind="baseline",
                experiment_id=plan.experiment_id,
                target_app=plan.target_app,
                target_repo=plan.target_repo,
            )
        )
        record.security_baseline = await self.agents.security.baseline(
            SecurityRequest(
                kind="baseline",
                experiment_id=plan.experiment_id,
                target_app=plan.target_app,
                target_repo=plan.target_repo,
            )
        )

        # Plugin's custom steady-state capture (informational; lands on
        # ctx.baseline for the plugin's own verify to compare against).
        await session.capture_baseline()

        if fail := safety.check_baseline_healthy(
            record.tester_baseline, record.security_baseline
        ):
            record.state = ExperimentState.BASELINE_FAIL
            return self._abort(record, fail.reason, fail.detail)
        record.state = ExperimentState.BASELINE_OK
        self._sync_plugin(record)
        self.store.save(record)

        if fail := self._check_budget_step(budget, record):
            return self._abort(record, fail.reason, fail.detail)
        if fail := await self._check_control(record):
            return self._abort(record, fail.reason, fail.detail)

        # --- inject ---------------------------------------------------------
        record.state = ExperimentState.INJECT
        self.store.save(record)

        # The plugin session drives the workload and polls its steady-state
        # guard concurrently with injection. With no plugin (or a plugin that
        # overrides neither), drive_run just awaits the injection.
        try:
            record.chaos_timeline = await session.drive_run(
                lambda: self.agents.chaos.execute(plan)
            )
        except GuardTripped as e:
            # The guard cancelled the injection mid-flight; the fault may still
            # be live. Best-effort cleanup so a tripped guard never leaves an
            # active fault behind, then gather evidence and abort.
            record.state = ExperimentState.INJECT_FAILED
            await self._best_effort_chaos_cleanup(plan)
            record.plugin_diagnostics = await session.collect_diagnostics()
            self._sync_plugin(record)
            return self._abort(record, AbortReason.SLO_BREACH, str(e))
        except Exception as e:
            record.state = ExperimentState.INJECT_FAILED
            return self._abort(record, AbortReason.AGENT_FAILURE, f"chaos.execute raised: {e!r}")

        if not record.chaos_timeline.success:
            record.state = ExperimentState.INJECT_FAILED
            return self._abort(
                record, AbortReason.AGENT_FAILURE, record.chaos_timeline.error or "unknown"
            )
        record.state = ExperimentState.INJECTED
        self.store.save(record)
        if fail := await self._check_control(record):
            return self._abort(record, fail.reason, fail.detail)

        # --- verify ---------------------------------------------------------
        record.state = ExperimentState.VERIFY
        self.store.save(record)

        record.tester_verify = await self.agents.tester.verify(
            TesterRequest(
                kind="verify",
                experiment_id=plan.experiment_id,
                target_app=plan.target_app,
                target_repo=plan.target_repo,
                baseline_samples=(
                    record.tester_baseline.samples if record.tester_baseline else []
                ),
            )
        )
        record.security_verify = await self.agents.security.verify(
            SecurityRequest(
                kind="verify",
                experiment_id=plan.experiment_id,
                target_app=plan.target_app,
                target_repo=plan.target_repo,
            )
        )

        # Plugin's custom validation — augments the built-in verify. A failed
        # VerifyResult marks the run regressed even when tester/security are
        # green; its structured failures are persisted for the audit trail.
        plugin_verify = await session.verify()
        record.verify_result = plugin_verify
        plugin_regressed = plugin_verify is not None and not plugin_verify.passed
        builtin_regressed = (
            not record.tester_verify.steady_state
            or record.security_verify.has_critical_or_high
            or record.security_verify.sbom_drift_from_baseline
        )
        regressed = builtin_regressed or plugin_regressed

        if not regressed:
            record.state = ExperimentState.STEADY
            self._sync_plugin(record)
            return self._finish(record)

        # Gather failure evidence before any teardown destroys it.
        record.plugin_diagnostics = await session.collect_diagnostics()

        if fail := self._check_budget_step(budget, record):
            return self._abort(record, fail.reason, fail.detail)
        if fail := await self._check_control(record):
            return self._abort(record, fail.reason, fail.detail)

        # --- diagnose -------------------------------------------------------
        record.state = ExperimentState.REGRESSED
        self._sync_plugin(record)
        self.store.save(record)

        # The built-in diagnostician consumes a failed tester/security report.
        # If the regression was detected *only* by the plugin's verify, there's
        # no such report to hand it — and the plugin already produced structured
        # failure details (verify_result + diagnostics). Record and finish
        # rather than invoke the generic diagnostician with nothing to chew on.
        if not builtin_regressed:
            log.info(
                "%s regression detected by plugin verify only; "
                "skipping diagnostician (see verify_result)",
                plan.experiment_id,
            )
            return self._finish(record)
        record.state = ExperimentState.DIAGNOSE
        self.store.save(record)  # persist before the long-running agent call
        record.diagnosis = await self.agents.diagnostician.diagnose(
            DiagnosisRequest(
                experiment_id=plan.experiment_id,
                failed_tester_report=(
                    record.tester_verify if not record.tester_verify.steady_state else None
                ),
                failed_security_report=(
                    record.security_verify
                    if record.security_verify.has_critical_or_high
                    or record.security_verify.sbom_drift_from_baseline
                    else None
                ),
                chaos_timeline=record.chaos_timeline,
                target_repo=plan.target_repo,
            )
        )
        record.state = ExperimentState.DIAGNOSED
        # Apply suppression after the diagnostician returns. Mutates the
        # report in place — every hypothesis stays in the audit trail, but
        # the suppressed ones won't trigger the fixer.
        active_suppressions = suppression.build_active_list(plan)
        suppression.apply_to_diagnosis(record.diagnosis, active_suppressions)
        self.store.save(record)

        if fail := self._check_budget_step(budget, record):
            return self._abort(record, fail.reason, fail.detail)
        if fail := await self._check_control(record):
            return self._abort(record, fail.reason, fail.detail)

        # --- propose fix ----------------------------------------------------
        # If every hypothesis was suppressed there's nothing actionable left
        # to propose a fix for. Transition to FIX_DECLINED with a note so the
        # audit trail captures the decline.
        if not suppression.active_hypotheses(record.diagnosis):
            log.info(
                "%s all hypotheses suppressed; skipping propose_fix",
                plan.experiment_id,
            )
            record.diagnosis.notes = (
                f"{record.diagnosis.notes}\n" if record.diagnosis.notes else ""
            ) + "all hypotheses suppressed by .chaos/suppress.yaml or plan.suppress"
            record.state = ExperimentState.FIX_DECLINED
            return self._finish(record)

        record.state = ExperimentState.PROPOSE_FIX
        self.store.save(record)  # persist before the long-running agent call
        record.fix_proposal = await self.agents.fixer.propose_fix(record.diagnosis)
        record.state = ExperimentState.FIX_PROPOSED
        return self._finish(record)

    # ----- helpers ----------------------------------------------------------

    async def _check_control(
        self, record: ExperimentRecord
    ) -> safety.GateFailure | None:
        """Poll the operator's pause / abort signals.

        Called between every state transition. Three outcomes:
            - abort requested ⇒ returns GateFailure (USER_KILL or whatever
              reason the operator set); caller routes to ``_abort``.
            - pause requested ⇒ mutates record.state to PAUSED, persists,
              sleeps ``pause_poll_interval_s`` and re-polls. Loops until
              pause clears OR an abort arrives.
            - neither ⇒ returns None. Caller proceeds.

        While paused, the orchestrator does no work. Resume continues from
        wherever the next state assignment lands — the PAUSED marker is
        ephemeral, overwritten by the next state transition.
        """
        first_check = True
        while True:
            ctrl = self.store.load_control(record.experiment_id)
            if ctrl.abort_requested:
                return safety.GateFailure(
                    ctrl.abort_reason or AbortReason.USER_KILL,
                    "abort requested by operator",
                )
            if not ctrl.pause_requested:
                return None
            # Pause requested. On the first iteration, mark + persist.
            if first_check:
                log.info(
                    "experiment %s pausing (was %s)",
                    record.experiment_id, record.state.value,
                )
                record.state = ExperimentState.PAUSED
                self._sync_spend(record)
                self.store.save(record)
                first_check = False
            await asyncio.sleep(self.pause_poll_interval_s)

    def _check_budget_step(
        self, budget: BudgetTracker, record: ExperimentRecord
    ) -> safety.GateFailure | None:
        """Refresh spend from the harness, persist it on the record, and gate.

        Called after every agent invocation that could spend tokens. Returns a
        `GateFailure` if the experiment must abort, else None.
        """
        if self.harness is not None:
            budget.spent_usd = sum(
                inv.spend_usd or 0.0 for inv in self.harness.invocations
            )
        record.spend_usd = budget.spent_usd
        if budget.soft_warn_due():
            log.warning(
                "experiment %s passed soft budget cap: $%.2f >= $%.2f",
                record.experiment_id, budget.spent_usd, budget.budget.soft_cap_usd,
            )
        if budget.hard_exceeded():
            return safety.GateFailure(
                AbortReason.BUDGET_EXCEEDED,
                f"hard cap: spent=${budget.spent_usd:.2f} cap=${budget.budget.hard_cap_usd:.2f} "
                f"elapsed={budget.elapsed_seconds():.0f}s wall_clock={budget.budget.wall_clock_seconds}s",
            )
        return None

    async def _fetch_namespace_annotations(self, namespace: str) -> dict[str, str] | None:
        """Ask the chaos agent for namespace annotations.

        Returns ``None`` if the agent doesn't expose the method (mocks, dry-run)
        — the safety gate then treats that as "unverifiable" and fails closed
        per ``check_namespace_annotation``'s contract.
        """
        getter = getattr(self.agents.chaos, "get_namespace_annotations", None)
        if getter is None:
            return None
        try:
            result: dict[str, str] | None = await getter(namespace)
        except Exception as e:
            log.warning("namespace annotation fetch failed: %r", e)
            return None
        return result

    def _attach_invocations(self, record: ExperimentRecord) -> None:
        """Copy the latest harness invocations onto the record before each save.

        Uses ``dataclasses.asdict`` so nested dataclasses (``ToolCallRecord``)
        flatten to plain dicts that Pydantic can validate into the contract
        types (``ToolCallSummary``). Lazy import keeps the orchestrator
        runnable without the harness (tests/dry-run).
        """
        if self.harness is None:
            return
        from dataclasses import asdict

        from shared.contracts import AgentInvocationLog

        record.agent_invocations = [
            AgentInvocationLog.model_validate(asdict(inv))
            for inv in self.harness.invocations
        ]

    async def _best_effort_chaos_cleanup(self, plan: ExperimentPlan) -> None:
        """Ask the chaos agent to delete any live faults for this plan. Never raises."""
        cleanup = getattr(self.agents.chaos, "cleanup", None)
        if cleanup is None:
            return
        try:
            await cleanup(plan)
        except Exception as e:  # cleanup is best-effort; never raise
            log.warning("best-effort chaos cleanup failed for %s: %r", plan.experiment_id, e)

    def _sync_plugin(self, record: ExperimentRecord) -> None:
        """Copy the live plugin session's audit state onto the record.

        Idempotent; called before each persistence point so the stored record
        reflects plugin progress (stage results, verdict, diagnostics). No-op
        when no plugin session is active.
        """
        session = self._session
        if session is None:
            return
        record.plugin_name = session.plugin_name
        record.plugin_stage_results = list(session.records)
        if session.verify_result is not None:
            record.verify_result = session.verify_result
        if session.diagnostics:
            record.plugin_diagnostics = session.diagnostics

    def _abort(
        self,
        record: ExperimentRecord,
        reason: AbortReason,
        detail: str,
    ) -> ExperimentRecord:
        log.warning("experiment %s aborted: %s — %s", record.experiment_id, reason, detail)
        record.state = ExperimentState.ABORTED
        record.abort_reason = reason
        record.abort_detail = detail
        record.finished_at = datetime.now(tz=UTC)
        self._attach_invocations(record)
        self._sync_spend(record)
        self._sync_plugin(record)
        self.store.save(record)
        # Clear any operator signals after the terminal save so a record
        # never persists with `pause_requested=1` or `abort_requested=1`
        # alongside a terminal state.
        self.store.clear_control(record.experiment_id)
        return record

    def _finish(self, record: ExperimentRecord) -> ExperimentRecord:
        record.state = ExperimentState.RECORDED
        record.finished_at = datetime.now(tz=UTC)
        self._attach_invocations(record)
        self._sync_spend(record)
        self._sync_plugin(record)
        self.store.save(record)
        self.store.clear_control(record.experiment_id)
        return record

    def _sync_spend(self, record: ExperimentRecord) -> None:
        """Make ``record.spend_usd`` reflect the final invocation log before save."""
        if self.harness is None:
            return
        record.spend_usd = sum(inv.spend_usd or 0.0 for inv in self.harness.invocations)
