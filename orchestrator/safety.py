"""
Blast-radius enforcement and abort conditions.

Every check here is deterministic Python. The orchestrator never delegates these
decisions to an LLM. Each function returns either None (gate passed) or an
AbortReason + detail string.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.contracts import (
    AbortReason,
    ExperimentPlan,
    SafetyConstraints,
    SecurityReport,
    TesterReport,
)


@dataclass(frozen=True)
class GateFailure:
    reason: AbortReason
    detail: str


def check_cluster_allowed(constraints: SafetyConstraints) -> GateFailure | None:
    """Refuse to run against clusters whose name matches the denylist."""
    ctx = constraints.cluster_context.lower()
    for forbidden in constraints.forbidden_cluster_substrings:
        if forbidden in ctx:
            return GateFailure(
                AbortReason.CLUSTER_DENIED,
                f"cluster context {constraints.cluster_context!r} matches forbidden "
                f"substring {forbidden!r}",
            )
    return None


def check_namespace_annotation(
    constraints: SafetyConstraints,
    annotations: dict[str, str],
) -> GateFailure | None:
    """Require explicit `chaos.kosta.dev/allowed: "true"` on the target namespace."""
    if not constraints.require_namespace_annotation:
        return None
    val = annotations.get("chaos.kosta.dev/allowed")
    if val != "true":
        return GateFailure(
            AbortReason.CLUSTER_DENIED,
            f"namespace {constraints.namespace!r} missing annotation "
            "chaos.kosta.dev/allowed=true",
        )
    return None


def check_blast_radius(plan: ExperimentPlan) -> GateFailure | None:
    """Validate the plan against its declared safety constraints."""
    c = plan.safety
    if len(plan.faults) > 1 and not c.allow_multi_fault:
        return GateFailure(
            AbortReason.BLAST_RADIUS_VIOLATION,
            f"plan has {len(plan.faults)} faults but allow_multi_fault is False",
        )
    for fault in plan.faults:
        if fault.duration_seconds > c.max_duration_seconds:
            return GateFailure(
                AbortReason.BLAST_RADIUS_VIOLATION,
                f"fault {fault.name!r} duration {fault.duration_seconds}s exceeds "
                f"max_duration_seconds {c.max_duration_seconds}s",
            )
    return None


def check_baseline_healthy(
    tester: TesterReport | None,
    security: SecurityReport | None,
) -> GateFailure | None:
    """Refuse to inject chaos if baseline already shows regression."""
    if tester is not None and not tester.steady_state:
        return GateFailure(
            AbortReason.BASELINE_UNHEALTHY,
            f"tester baseline not steady: {tester.failed_probes or tester.anomalies}",
        )
    if security is not None and security.has_critical_or_high:
        return GateFailure(
            AbortReason.BASELINE_UNHEALTHY,
            f"security baseline has critical/high findings: "
            f"{[f.title for f in security.findings if f.severity in ('high', 'critical')]}",
        )
    return None


def check_budget(spent_usd: float, hard_cap_usd: float) -> GateFailure | None:
    if spent_usd >= hard_cap_usd:
        return GateFailure(
            AbortReason.BUDGET_EXCEEDED,
            f"spent ${spent_usd:.2f} >= hard cap ${hard_cap_usd:.2f}",
        )
    return None
