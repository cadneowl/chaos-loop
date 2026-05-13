/**
 * TypeScript mirror of `shared/contracts.py` — same hand-maintained file as
 * `ui/server/src/contracts/index.ts`.
 *
 * Kept duplicated (rather than referenced via a shared package) because:
 *   - Angular's strict module boundaries don't love importing from outside
 *     the project's source tree
 *   - The `pnpm` workspace can hoist a third package eventually if the
 *     drift becomes painful
 *   - For now we have ~270 lines of pure type declarations; mirroring is
 *     cheap and transparent
 *
 * If you change `shared/contracts.py`, mirror it in BOTH this file and
 * `ui/server/src/contracts/index.ts`. CONTRIBUTING.md captures the rule.
 */

export type ExperimentId = string;
export type RunId = string;
export type HypothesisId = string;
export type FindingId = string;
export type IsoDatetime = string;

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
  | 'pod' | 'network' | 'io' | 'stress' | 'dns' | 'http' | 'time' | 'kernel'
  | 'cert' | 'tls' | 'auth' | 'secret' | 'image' | 'iam' | 'netpol'
  | 'egress' | 'runtime';

export type FindingSeverity = 'info' | 'low' | 'medium' | 'high' | 'critical';

export type FixAction = 'none' | 'doc-only' | 'code-patch' | 'config-change';

export type SuggestedFixClass =
  | 'code-patch' | 'config-change'
  | 'missing-retry' | 'missing-timeout' | 'missing-circuit-breaker' | 'missing-fallback'
  | 'auth-control-gap' | 'secret-handling' | 'image-policy'
  | 'test-gap' | 'working-as-intended';

export type ScannerName =
  | 'zap' | 'syft' | 'grype' | 'trivy' | 'gitleaks'
  | 'cosign' | 'kubescape' | 'custom-probe';

export type RequestKind = 'baseline' | 'verify' | 'drift' | 'hypothesize';

export type TimelineEventKind =
  | 'scheduled' | 'started' | 'verified-active' | 'stopped' | 'cleaned-up' | 'error';

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

export interface RootCauseHypothesis {
  summary: string;
  confidence: number;
  evidence: string[];
  suggested_fix_class: SuggestedFixClass;
  affected_paths: string[];
  /**
   * Stable 12-hex fingerprint derived from (fix_class, sorted paths, summary).
   * Match against `DiagnosisReport.suppressed_fingerprints` to know whether
   * this hypothesis was muted.
   */
  id?: string;
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

export interface ControlSignals {
  pause_requested: boolean;
  abort_requested: boolean;
  abort_reason: AbortReason | null;
}

// =============================================================================
// API response shapes (specific to /api/v1)
// =============================================================================

/** Slim row used by GET /api/v1/experiments. */
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
// Cross-experiment aggregates (mirror of ui/server/src/aggregates/aggregates.types.ts)
// =============================================================================

export interface LlmAggregates {
  totals: {
    spend_usd: number;
    prompt_tokens: number;
    completion_tokens: number;
    experiments: number;
    invocations: number;
    invocations_with_llm: number;
  };
  by_agent: Array<{
    agent: string;
    spend_usd: number;
    prompt_tokens: number;
    completion_tokens: number;
    invocations: number;
  }>;
  by_experiment: Array<{
    experiment_id: string;
    title: string;
    started_at: IsoDatetime;
    spend_usd: number;
    tokens: number;
  }>;
}

export interface FindingsAggregates {
  totals: {
    experiments_with_diagnosis: number;
    total_hypotheses: number;
    mean_confidence: number;
  };
  by_fix_class: Array<{
    fix_class: SuggestedFixClass;
    count: number;
    mean_confidence: number;
  }>;
  confidence_histogram: Array<{ bucket: string; count: number }>;
  recent: Array<{
    experiment_id: string;
    summary: string;
    confidence: number;
    fix_class: SuggestedFixClass;
    started_at: IsoDatetime;
  }>;
}

export interface FixesAggregates {
  totals: {
    fix_proposals: number;
    with_pr: number;
    with_regression_test: number;
    mean_confidence: number;
  };
  by_action: Array<{ action: FixAction; count: number }>;
  by_file: Array<{ path: string; count: number }>;
  by_day: Array<{ date: string; count: number }>;
}

// =============================================================================
// Control plane
// =============================================================================

export type RunProfile = 'static' | 'hybrid' | 'llm';

export interface PlanFile {
  filename: string;
  experiment_id: string;
  title: string;
  target_app: string;
}

export interface RunResponse {
  experiment_id: string;
  title: string;
  target_app: string;
  profile: RunProfile;
}
