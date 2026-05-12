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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from orchestrator import safety
from orchestrator.budget import BudgetTracker
from orchestrator.store import ExperimentStore
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
    ) -> None:
        self.agents = agents
        self.store = store
        self.harness = harness

    async def run(self, plan: ExperimentPlan) -> ExperimentRecord:
        record = ExperimentRecord(
            experiment_id=plan.experiment_id,
            plan=plan,
            state=ExperimentState.INITIALIZING,
        )
        self._attach_invocations(record)
        self.store.save(record)
        budget = BudgetTracker(plan.budget)

        # --- pre-flight safety gates ----------------------------------------
        if fail := safety.check_cluster_allowed(plan.safety):
            return self._abort(record, fail.reason, fail.detail)
        if fail := safety.check_blast_radius(plan):
            return self._abort(record, fail.reason, fail.detail)
        if plan.safety.require_namespace_annotation:
            annotations = await self._fetch_namespace_annotations(plan.safety.namespace)
            if fail := safety.check_namespace_annotation(plan.safety, annotations):
                return self._abort(record, fail.reason, fail.detail)

        # --- baseline -------------------------------------------------------
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

        if fail := safety.check_baseline_healthy(
            record.tester_baseline, record.security_baseline
        ):
            record.state = ExperimentState.BASELINE_FAIL
            return self._abort(record, fail.reason, fail.detail)
        record.state = ExperimentState.BASELINE_OK
        self.store.save(record)

        if fail := self._check_budget_step(budget, record):
            return self._abort(record, fail.reason, fail.detail)

        # --- inject ---------------------------------------------------------
        record.state = ExperimentState.INJECT
        self.store.save(record)

        try:
            record.chaos_timeline = await self.agents.chaos.execute(plan)
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

        regressed = (
            not record.tester_verify.steady_state
            or record.security_verify.has_critical_or_high
            or record.security_verify.sbom_drift_from_baseline
        )

        if not regressed:
            record.state = ExperimentState.STEADY
            return self._finish(record)

        if fail := self._check_budget_step(budget, record):
            return self._abort(record, fail.reason, fail.detail)

        # --- diagnose -------------------------------------------------------
        record.state = ExperimentState.REGRESSED
        self.store.save(record)
        record.state = ExperimentState.DIAGNOSE
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
        self.store.save(record)

        if fail := self._check_budget_step(budget, record):
            return self._abort(record, fail.reason, fail.detail)

        # --- propose fix ----------------------------------------------------
        record.state = ExperimentState.PROPOSE_FIX
        record.fix_proposal = await self.agents.fixer.propose_fix(record.diagnosis)
        record.state = ExperimentState.FIX_PROPOSED
        return self._finish(record)

    # ----- helpers ----------------------------------------------------------

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

        Re-imports lazily to avoid a hard dep on the harness from the loop:
        the orchestrator can run without one (tests/dry-run).
        """
        if self.harness is None:
            return
        from shared.contracts import AgentInvocationLog

        record.agent_invocations = [
            AgentInvocationLog(**vars(inv)) for inv in self.harness.invocations
        ]

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
        self.store.save(record)
        return record

    def _finish(self, record: ExperimentRecord) -> ExperimentRecord:
        record.state = ExperimentState.RECORDED
        record.finished_at = datetime.now(tz=UTC)
        self._attach_invocations(record)
        self._sync_spend(record)
        self.store.save(record)
        return record

    def _sync_spend(self, record: ExperimentRecord) -> None:
        """Make ``record.spend_usd`` reflect the final invocation log before save."""
        if self.harness is None:
            return
        record.spend_usd = sum(inv.spend_usd or 0.0 for inv in self.harness.invocations)
