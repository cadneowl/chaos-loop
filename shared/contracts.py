"""
Pydantic contracts shared across agents.

Stability policy: changes to this file are interface changes. Bump the project version
and write a migration note in CHANGELOG.md before merging. Agents validate every
input and output against these schemas; an agent that emits malformed output is
treated by the orchestrator as a failure, not a partial success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------- identifiers --------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


ExperimentId = Annotated[str, Field(pattern=r"^exp-[0-9a-f]{12}$")]
RunId = Annotated[str, Field(pattern=r"^run-[0-9a-f]{12}$")]
HypothesisId = Annotated[str, Field(pattern=r"^h-[0-9a-z\-]{1,64}$")]
FindingId = Annotated[str, Field(pattern=r"^f-[0-9a-z\-]{1,64}$")]


class AgentKind(StrEnum):
    TESTER = "tester"
    CHAOS = "chaos"
    SECURITY = "security"
    DIAGNOSTICIAN = "diagnostician"
    FIXER = "fixer"


# ---------- safety + budget ----------------------------------------------------


class SafetyConstraints(BaseModel):
    """Hard caps the orchestrator enforces before invoking the chaos agent."""

    model_config = ConfigDict(frozen=True)

    cluster_context: str
    namespace: str
    max_pods_affected: int = Field(default=1, ge=1, le=100)
    max_duration_seconds: int = Field(default=300, ge=1, le=3600)
    allow_multi_fault: bool = False
    require_namespace_annotation: bool = True
    forbidden_cluster_substrings: tuple[str, ...] = ("prod", "production", "live", "main")
    abort_slo_query: str | None = None
    abort_slo_threshold: float | None = None


class TokenBudget(BaseModel):
    """Per-experiment cost ceiling. Orchestrator aborts on hard cap."""

    soft_cap_usd: float = Field(default=2.0, gt=0)
    hard_cap_usd: float = Field(default=10.0, gt=0)
    wall_clock_seconds: int = Field(default=1800, gt=0)

    @field_validator("hard_cap_usd")
    @classmethod
    def _hard_above_soft(cls, v: float, info: Any) -> float:
        soft = info.data.get("soft_cap_usd", 0)
        if v < soft:
            raise ValueError("hard_cap_usd must be >= soft_cap_usd")
        return v


class AbortReason(StrEnum):
    BASELINE_UNHEALTHY = "baseline_unhealthy"
    SLO_BREACH = "slo_breach"
    BUDGET_EXCEEDED = "budget_exceeded"
    USER_KILL = "user_kill"
    BLAST_RADIUS_VIOLATION = "blast_radius_violation"
    CLUSTER_DENIED = "cluster_denied"
    APPROVAL_REJECTED = "approval_rejected"
    AGENT_FAILURE = "agent_failure"


# ---------- faults -------------------------------------------------------------


class FaultCategory(StrEnum):
    POD = "pod"
    NETWORK = "network"
    IO = "io"
    STRESS = "stress"
    DNS = "dns"
    HTTP = "http"
    TIME = "time"
    KERNEL = "kernel"
    # Security-flavored
    CERT = "cert"
    TLS = "tls"
    AUTH = "auth"
    SECRET = "secret"
    IMAGE = "image"
    IAM = "iam"
    NETPOL = "netpol"
    EGRESS = "egress"
    RUNTIME = "runtime"


class FaultSpec(BaseModel):
    """Declarative description of a single fault, before it's rendered to a CRD."""

    category: FaultCategory
    name: str = Field(min_length=1, max_length=128)
    target_selector: dict[str, str] = Field(
        description="Label selector or similar; concrete form depends on category"
    )
    parameters: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: int = Field(ge=1, le=3600)
    requires_approval: bool = False
    rationale: str = Field(min_length=1, description="Why are we injecting this?")
    hypothesis_id: HypothesisId | None = None


# ---------- experiment plan ----------------------------------------------------


class ExperimentPlan(BaseModel):
    """The orchestrator's request to the chaos agent."""

    experiment_id: ExperimentId = Field(default_factory=lambda: _new_id("exp"))
    title: str
    target_app: str
    target_repo: str | None = Field(default=None, description="git URL of target's source")
    faults: list[FaultSpec]
    safety: SafetyConstraints
    budget: TokenBudget = Field(default_factory=TokenBudget)
    quiet_window_pre_seconds: int = Field(default=60, ge=0)
    quiet_window_post_seconds: int = Field(default=60, ge=0)
    created_at: datetime = Field(default_factory=_now)

    @field_validator("faults")
    @classmethod
    def _at_least_one(cls, v: list[FaultSpec]) -> list[FaultSpec]:
        if len(v) < 1:
            raise ValueError("plan must include at least one fault")
        return v


# ---------- tester -------------------------------------------------------------


class StatisticalSample(BaseModel):
    """A distribution captured during baseline."""

    metric: str
    samples: list[float]
    mean: float
    p50: float
    p95: float
    p99: float
    stdev: float

    @classmethod
    def from_samples(cls, metric: str, samples: list[float]) -> StatisticalSample:
        if not samples:
            raise ValueError("need at least one sample")
        s = sorted(samples)
        n = len(s)
        mean = sum(s) / n
        var = sum((x - mean) ** 2 for x in s) / n
        return cls(
            metric=metric,
            samples=samples,
            mean=mean,
            p50=s[n // 2],
            p95=s[min(n - 1, int(n * 0.95))],
            p99=s[min(n - 1, int(n * 0.99))],
            stdev=var**0.5,
        )


class Hypothesis(BaseModel):
    """A testable claim about the system. Generated by tester or human."""

    id: HypothesisId
    statement: str
    rationale: str
    proposed_fault: str = Field(description="Name of fault from the catalogue")
    success_criteria: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    code_references: list[str] = Field(default_factory=list)


class TesterRequest(BaseModel):
    __test__ = False  # not a pytest test class

    kind: Literal["baseline", "verify", "hypothesize"]
    experiment_id: ExperimentId
    target_app: str
    target_repo: str | None = None
    baseline_run_count: int = Field(default=5, ge=1, le=50)
    probes: list[str] = Field(
        default_factory=list,
        description="Identifiers for probe suites to run; empty = use defaults",
    )


class TesterReport(BaseModel):
    __test__ = False  # not a pytest test class

    request_kind: Literal["baseline", "verify", "hypothesize"]
    experiment_id: ExperimentId
    run_id: RunId = Field(default_factory=lambda: _new_id("run"))
    steady_state: bool
    samples: list[StatisticalSample] = Field(default_factory=list)
    failed_probes: list[str] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    generated_hypotheses: list[Hypothesis] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    notes: str = ""


# ---------- security -----------------------------------------------------------


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SecurityFinding(BaseModel):
    id: FindingId
    severity: FindingSeverity
    title: str
    description: str
    scanner: Literal[
        "zap",
        "syft",
        "grype",
        "trivy",
        "gitleaks",
        "cosign",
        "kubescape",
        "custom-probe",
    ]
    cve: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    location: str | None = None


class SecurityHypothesis(BaseModel):
    """Like Hypothesis but specifically targeted at security properties."""

    id: HypothesisId
    statement: str
    rationale: str
    proposed_fault: str
    success_criteria: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    references: list[str] = Field(default_factory=list)


class SecurityRequest(BaseModel):
    kind: Literal["baseline", "verify", "drift", "hypothesize"]
    experiment_id: ExperimentId
    target_app: str
    target_repo: str | None = None
    target_images: list[str] = Field(default_factory=list)
    target_endpoints: list[str] = Field(default_factory=list)
    enable_active_dast: bool = False


class SecurityReport(BaseModel):
    request_kind: Literal["baseline", "verify", "drift", "hypothesize"]
    experiment_id: ExperimentId
    run_id: RunId = Field(default_factory=lambda: _new_id("run"))
    findings: list[SecurityFinding] = Field(default_factory=list)
    generated_hypotheses: list[SecurityHypothesis] = Field(default_factory=list)
    sbom_digest: str | None = None
    sbom_drift_from_baseline: bool = False
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    @property
    def has_critical_or_high(self) -> bool:
        return any(
            f.severity in (FindingSeverity.HIGH, FindingSeverity.CRITICAL) for f in self.findings
        )


# ---------- chaos timeline -----------------------------------------------------


class TimelineEvent(BaseModel):
    timestamp: datetime
    fault_name: str
    event: Literal["scheduled", "started", "verified-active", "stopped", "cleaned-up", "error"]
    detail: str = ""


class ChaosTimeline(BaseModel):
    experiment_id: ExperimentId
    events: list[TimelineEvent]
    success: bool
    error: str | None = None


# ---------- diagnostician ------------------------------------------------------


class DiagnosisRequest(BaseModel):
    experiment_id: ExperimentId
    failed_tester_report: TesterReport | None = None
    failed_security_report: SecurityReport | None = None
    chaos_timeline: ChaosTimeline
    target_repo: str | None = None

    @field_validator("failed_security_report")
    @classmethod
    def _at_least_one_failure(
        cls, v: SecurityReport | None, info: Any
    ) -> SecurityReport | None:
        tester = info.data.get("failed_tester_report")
        if v is None and tester is None:
            raise ValueError("DiagnosisRequest needs at least one failed report")
        return v


class RootCauseHypothesis(BaseModel):
    """One candidate explanation for the observed regression."""

    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    suggested_fix_class: Literal[
        "code-patch",
        "config-change",
        "missing-retry",
        "missing-timeout",
        "missing-circuit-breaker",
        "missing-fallback",
        "auth-control-gap",
        "secret-handling",
        "image-policy",
        "test-gap",
        "working-as-intended",
    ]
    affected_paths: list[str] = Field(default_factory=list)


class DiagnosisReport(BaseModel):
    experiment_id: ExperimentId
    run_id: RunId = Field(default_factory=lambda: _new_id("run"))
    hypotheses: list[RootCauseHypothesis]
    notes: str = ""
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    @field_validator("hypotheses")
    @classmethod
    def _nonempty(cls, v: list[RootCauseHypothesis]) -> list[RootCauseHypothesis]:
        if not v:
            raise ValueError("DiagnosisReport must include at least one hypothesis")
        return v


# ---------- fixer --------------------------------------------------------------


class FixAction(StrEnum):
    CODE_PATCH = "code-patch"
    CONFIG_CHANGE = "config-change"
    DOC_ONLY = "doc-only"
    NONE = "none"


class FixProposal(BaseModel):
    experiment_id: ExperimentId
    run_id: RunId = Field(default_factory=lambda: _new_id("run"))
    action: FixAction
    pr_url: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    files_touched: list[str] = Field(default_factory=list)
    regression_test_added: bool = False
    is_draft: bool = True
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    @field_validator("is_draft")
    @classmethod
    def _always_draft(cls, v: bool) -> bool:
        if v is False:
            raise ValueError("FixProposal.is_draft must be True; fixer never opens non-draft PRs")
        return v


# ---------- experiment record (persistence) -----------------------------------


class ExperimentState(StrEnum):
    INITIALIZING = "initializing"
    BASELINE = "baseline"
    BASELINE_OK = "baseline_ok"
    BASELINE_FAIL = "baseline_fail"
    INJECT = "inject"
    INJECTED = "injected"
    INJECT_FAILED = "inject_failed"
    VERIFY = "verify"
    STEADY = "steady"
    REGRESSED = "regressed"
    DIAGNOSE = "diagnose"
    DIAGNOSED = "diagnosed"
    PROPOSE_FIX = "propose_fix"
    FIX_PROPOSED = "fix_proposed"
    FIX_DECLINED = "fix_declined"
    ABORTED = "aborted"
    RECORDED = "recorded"


class ExperimentRecord(BaseModel):
    """The full audit trail of one experiment run. Persisted to SQLite."""

    experiment_id: ExperimentId
    plan: ExperimentPlan
    state: ExperimentState
    tester_baseline: TesterReport | None = None
    security_baseline: SecurityReport | None = None
    chaos_timeline: ChaosTimeline | None = None
    tester_verify: TesterReport | None = None
    security_verify: SecurityReport | None = None
    diagnosis: DiagnosisReport | None = None
    fix_proposal: FixProposal | None = None
    abort_reason: AbortReason | None = None
    abort_detail: str = ""
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    spend_usd: float = 0.0
