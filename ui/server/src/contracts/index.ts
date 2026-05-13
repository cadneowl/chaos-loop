/**
 * TypeScript mirror of `shared/contracts.py`.
 *
 * Hand-maintained on purpose — we tried codegen via Pydantic JSON Schema +
 * json-schema-to-typescript and the output was noisy (one synthetic alias
 * per nested property, name collisions like `ExperimentId` vs
 * `ExperimentId1`). Hand-writing is 27 short types, fully readable, and a
 * single property added to a Pydantic model takes ~10 seconds to mirror.
 *
 * The drift risk is contained by `contracts.spec.ts` next to this file,
 * which loads a real `ExperimentRecord` JSON dump and asserts every
 * required field is present at the type's expected location.
 *
 * If you change `shared/contracts.py`, mirror it in BOTH:
 *   - `ui/server/src/contracts/index.ts`
 *   - `ui/web/src/app/core/contracts.ts`
 * See CONTRIBUTING.md.
 */

// =============================================================================
// IDs and primitives
// =============================================================================

/** Pattern: `^exp-[0-9a-f]{12}$` */
export type ExperimentId = string;
/** Pattern: `^run-[0-9a-f]{12}$` */
export type RunId = string;
/** Pattern: `^h-[0-9a-z\-]{1,64}$` */
export type HypothesisId = string;
/** Pattern: `^f-[0-9a-z\-]{1,64}$` */
export type FindingId = string;

/** ISO-8601 UTC datetime (e.g. `2026-05-12T15:04:05.789Z`) */
export type IsoDatetime = string;

// =============================================================================
// Enums (mirror StrEnum on the Python side)
// =============================================================================

export type AgentKind = 'tester' | 'chaos' | 'security' | 'diagnostician' | 'fixer';

export type ExperimentState =
  | 'initializing'
  | 'baseline'
  | 'baseline_ok'
  | 'baseline_fail'
  | 'inject'
  | 'injected'
  | 'inject_failed'
  | 'verify'
  | 'steady'
  | 'regressed'
  | 'diagnose'
  | 'diagnosed'
  | 'propose_fix'
  | 'fix_proposed'
  | 'fix_declined'
  | 'paused'
  | 'aborted'
  | 'recorded';

export type AbortReason =
  | 'baseline_unhealthy'
  | 'slo_breach'
  | 'budget_exceeded'
  | 'user_kill'
  | 'blast_radius_violation'
  | 'cluster_denied'
  | 'approval_rejected'
  | 'agent_failure';

export type FaultCategory =
  | 'pod'
  | 'network'
  | 'io'
  | 'stress'
  | 'dns'
  | 'http'
  | 'time'
  | 'kernel'
  | 'cert'
  | 'tls'
  | 'auth'
  | 'secret'
  | 'image'
  | 'iam'
  | 'netpol'
  | 'egress'
  | 'runtime';

export type FindingSeverity = 'info' | 'low' | 'medium' | 'high' | 'critical';

export type FixAction =
  | 'none'
  | 'doc-only'
  | 'code-patch'
  | 'config-change';

export type SuggestedFixClass =
  | 'code-patch'
  | 'config-change'
  | 'missing-retry'
  | 'missing-timeout'
  | 'missing-circuit-breaker'
  | 'missing-fallback'
  | 'auth-control-gap'
  | 'secret-handling'
  | 'image-policy'
  | 'test-gap'
  | 'working-as-intended';

export type ScannerName =
  | 'zap'
  | 'syft'
  | 'grype'
  | 'trivy'
  | 'gitleaks'
  | 'cosign'
  | 'kubescape'
  | 'custom-probe';

export type RequestKind = 'baseline' | 'verify' | 'drift' | 'hypothesize';

export type TimelineEventKind =
  | 'scheduled'
  | 'started'
  | 'verified-active'
  | 'stopped'
  | 'cleaned-up'
  | 'error';

// =============================================================================
// Safety + budget
// =============================================================================

export interface SafetyConstraints {
  cluster_context: string;
  namespace: string;
  max_pods_affected: number;
  max_duration_seconds: number;
  allow_multi_fault: boolean;
  require_namespace_annotation: boolean;
  forbidden_cluster_substrings: string[];
}

export interface TokenBudget {
  soft_cap_usd: number;
  hard_cap_usd: number;
  wall_clock_seconds: number;
}

// =============================================================================
// Fault catalogue
// =============================================================================

export interface FaultSpec {
  category: FaultCategory;
  name: string;
  target_selector: Record<string, string>;
  parameters: Record<string, unknown>;
  duration_seconds: number;
  requires_approval: boolean;
  rationale: string;
  hypothesis_id?: HypothesisId | null;
}

// =============================================================================
// Experiment plan
// =============================================================================

export interface ExperimentPlan {
  experiment_id: ExperimentId;
  title: string;
  target_app: string;
  target_repo?: string | null;
  faults: FaultSpec[];
  safety: SafetyConstraints;
  budget: TokenBudget;
  quiet_window_pre_seconds: number;
  quiet_window_post_seconds: number;
  created_at: IsoDatetime;
}

// =============================================================================
// Chaos timeline
// =============================================================================

export interface TimelineEvent {
  timestamp: IsoDatetime;
  fault_name: string;
  event: TimelineEventKind;
  detail: string;
}

export interface ChaosTimeline {
  experiment_id: ExperimentId;
  events: TimelineEvent[];
  success: boolean;
  error?: string | null;
}

// =============================================================================
// Tester
// =============================================================================

export interface StatisticalSample {
  metric: string;
  samples: number[];
  mean: number;
  p50: number;
  p95: number;
  p99: number;
  stdev: number;
}

export interface Hypothesis {
  id: HypothesisId;
  statement: string;
  rationale: string;
  proposed_fault: string;
  success_criteria: string[];
  confidence: number;
  code_references: string[];
}

export interface TesterRequest {
  kind: RequestKind;
  experiment_id: ExperimentId;
  target_app: string;
  target_repo?: string | null;
  baseline_samples: StatisticalSample[];
  baseline_run_count: number;
}

export interface TesterReport {
  request_kind: RequestKind;
  experiment_id: ExperimentId;
  run_id: RunId;
  steady_state: boolean;
  samples: StatisticalSample[];
  failed_probes: string[];
  anomalies: string[];
  generated_hypotheses: Hypothesis[];
  started_at: IsoDatetime;
  finished_at?: IsoDatetime | null;
  notes: string;
}

// =============================================================================
// Security
// =============================================================================

export interface SecurityFinding {
  id: FindingId;
  severity: FindingSeverity;
  title: string;
  description: string;
  scanner: ScannerName;
  cve?: string | null;
  evidence: Record<string, unknown>;
  location?: string | null;
}

export interface SecurityHypothesis {
  id: HypothesisId;
  statement: string;
  rationale: string;
  proposed_fault: string;
  success_criteria: string[];
  confidence: number;
  references: string[];
}

export interface SecurityRequest {
  kind: RequestKind;
  experiment_id: ExperimentId;
  target_app: string;
  target_repo?: string | null;
  target_images: string[];
  target_endpoints: string[];
  enable_active_dast: boolean;
}

export interface SecurityReport {
  request_kind: RequestKind;
  experiment_id: ExperimentId;
  run_id: RunId;
  findings: SecurityFinding[];
  generated_hypotheses: SecurityHypothesis[];
  sbom_digest?: string | null;
  sbom_drift_from_baseline: boolean;
  started_at: IsoDatetime;
  finished_at?: IsoDatetime | null;
}

// =============================================================================
// Diagnosis
// =============================================================================

export interface RootCauseHypothesis {
  summary: string;
  confidence: number;
  evidence: string[];
  suggested_fix_class: SuggestedFixClass;
  affected_paths: string[];
  /**
   * Stable 12-hex fingerprint derived from (fix_class, sorted paths, summary).
   * Match against `DiagnosisReport.suppressed_fingerprints` to know whether
   * this hypothesis was muted. Computed on the server side; not optional in
   * practice but typed optional for backwards compatibility with records
   * written before this field was added.
   */
  id?: string;
}

export interface DiagnosisRequest {
  experiment_id: ExperimentId;
  failed_tester_report?: TesterReport | null;
  failed_security_report?: SecurityReport | null;
  chaos_timeline: ChaosTimeline;
  target_repo?: string | null;
}

export interface DiagnosisReport {
  experiment_id: ExperimentId;
  run_id: RunId;
  hypotheses: RootCauseHypothesis[];
  notes: string;
  started_at: IsoDatetime;
  finished_at?: IsoDatetime | null;
  /** 12-hex fingerprints of hypotheses muted by .chaos/suppress.yaml or plan.suppress. */
  suppressed_fingerprints?: string[];
  /** fingerprint → rule.reason for the muted hypotheses above. */
  suppression_notes?: Record<string, string>;
}

// =============================================================================
// Fix proposal
// =============================================================================

export interface FixProposal {
  experiment_id: ExperimentId;
  run_id: RunId;
  action: FixAction;
  pr_url?: string | null;
  confidence: number;
  reasoning: string;
  files_touched: string[];
  regression_test_added: boolean;
  is_draft: boolean;
  started_at: IsoDatetime;
  finished_at?: IsoDatetime | null;
}

// =============================================================================
// Audit trail
// =============================================================================

export interface ToolCallSummary {
  name: string;
  arguments: string;
  result_preview: string;
  is_error: boolean;
}

export interface AgentInvocationLog {
  agent: string;
  method: string;
  started_at_ms: number;
  finished_at_ms?: number | null;
  duration_ms?: number | null;
  ok: boolean;
  error?: string | null;
  input_summary: string;
  output_summary: string;
  spend_usd?: number | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  tool_calls: ToolCallSummary[];
}

// =============================================================================
// Experiment record (the central type)
// =============================================================================

export interface ExperimentRecord {
  experiment_id: ExperimentId;
  plan: ExperimentPlan;
  state: ExperimentState;
  tester_baseline?: TesterReport | null;
  security_baseline?: SecurityReport | null;
  chaos_timeline?: ChaosTimeline | null;
  tester_verify?: TesterReport | null;
  security_verify?: SecurityReport | null;
  diagnosis?: DiagnosisReport | null;
  fix_proposal?: FixProposal | null;
  abort_reason?: AbortReason | null;
  abort_detail: string;
  started_at: IsoDatetime;
  finished_at?: IsoDatetime | null;
  spend_usd: number;
  agent_invocations: AgentInvocationLog[];
}

// =============================================================================
// Control signals (mirror orchestrator/store.py ControlSignals)
// =============================================================================

export interface ControlSignals {
  pause_requested: boolean;
  abort_requested: boolean;
  abort_reason: AbortReason | null;
}

// =============================================================================
// API response shapes — used by /api/v1 endpoints. Mirror what the controller
// emits so consumers (Angular, future SDKs) share one definition.
// =============================================================================

export interface ExperimentSummary {
  experiment_id: ExperimentId;
  title: string;
  target_app: string;
  state: ExperimentState;
  started_at: IsoDatetime;
  finished_at: IsoDatetime | null;
  duration_seconds: number | null;
  spend_usd: number;
  abort_reason: string | null;
  primary_fault: string | null;
  is_paused: boolean;
}

export interface ExperimentListResponse {
  results: ExperimentSummary[];
  total: number;
  limit: number;
  offset: number;
}

// =============================================================================
// Control plane
// =============================================================================

/** Strategy mix the orchestrator accepts on `chaos run --profile`. */
export type RunProfile = 'static' | 'hybrid' | 'llm';
