"""Safety-gate unit tests. These run without any cluster or agent."""

from __future__ import annotations

from orchestrator import safety
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    FindingSeverity,
    SafetyConstraints,
    SecurityFinding,
    SecurityReport,
    TesterReport,
)


def _plan(**overrides) -> ExperimentPlan:
    base = dict(
        title="test",
        target_app="otel-demo",
        faults=[
            FaultSpec(
                category=FaultCategory.NETWORK,
                name="network.loss",
                target_selector={"app": "x"},
                duration_seconds=30,
                rationale="test",
            )
        ],
        safety=SafetyConstraints(cluster_context="kind-chaos", namespace="otel-demo"),
    )
    base.update(overrides)
    return ExperimentPlan(**base)


def test_cluster_allowed_rejects_prod() -> None:
    fail = safety.check_cluster_allowed(
        SafetyConstraints(cluster_context="prod-east", namespace="x")
    )
    assert fail is not None and fail.reason.value == "cluster_denied"


def test_cluster_allowed_accepts_kind() -> None:
    assert safety.check_cluster_allowed(
        SafetyConstraints(cluster_context="kind-chaos", namespace="x")
    ) is None


def test_namespace_annotation_required() -> None:
    c = SafetyConstraints(cluster_context="kind-chaos", namespace="x")
    assert safety.check_namespace_annotation(c, {}) is not None
    assert safety.check_namespace_annotation(c, {"chaos.kosta.dev/allowed": "false"}) is not None
    assert safety.check_namespace_annotation(c, {"chaos.kosta.dev/allowed": "true"}) is None


def test_blast_radius_multi_fault_rejected_by_default() -> None:
    plan = _plan(
        faults=[
            FaultSpec(
                category=FaultCategory.NETWORK,
                name="network.loss",
                target_selector={"app": "x"},
                duration_seconds=30,
                rationale="r",
            ),
            FaultSpec(
                category=FaultCategory.POD,
                name="pod.kill",
                target_selector={"app": "y"},
                duration_seconds=30,
                rationale="r",
            ),
        ]
    )
    fail = safety.check_blast_radius(plan)
    assert fail is not None and fail.reason.value == "blast_radius_violation"


def test_blast_radius_duration_cap() -> None:
    plan = _plan(
        faults=[
            FaultSpec(
                category=FaultCategory.NETWORK,
                name="network.loss",
                target_selector={"app": "x"},
                duration_seconds=600,  # exceeds default 300
                rationale="r",
            )
        ]
    )
    fail = safety.check_blast_radius(plan)
    assert fail is not None and fail.reason.value == "blast_radius_violation"


def test_baseline_unhealthy_blocks() -> None:
    tester = TesterReport(
        request_kind="baseline",
        experiment_id="exp-aaaaaaaaaaaa",
        steady_state=False,
        failed_probes=["x"],
    )
    fail = safety.check_baseline_healthy(tester, None)
    assert fail is not None and fail.reason.value == "baseline_unhealthy"


def test_baseline_critical_security_finding_blocks() -> None:
    sec = SecurityReport(
        request_kind="baseline",
        experiment_id="exp-aaaaaaaaaaaa",
        findings=[
            SecurityFinding(
                id="f-1",
                severity=FindingSeverity.CRITICAL,
                title="CVE-2025-0001 in libfoo",
                description="rce",
                scanner="grype",
            )
        ],
    )
    fail = safety.check_baseline_healthy(None, sec)
    assert fail is not None
