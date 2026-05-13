/**
 * Cross-experiment aggregate response shapes.
 *
 * Hand-mirrored on the client side in
 * `ui/web/src/app/core/contracts.ts`. If you change a field, change both.
 *
 * Each projection is shaped to be plotted directly by ECharts without a
 * second pass: arrays of `{ name, value }` for categorical charts, arrays
 * of `[x, y]` tuples never appear here (we let the client map them) but
 * the data is grouped so the client transform is `O(n)` and stateless.
 */

import type { FixAction, IsoDatetime, SuggestedFixClass } from '../contracts';

/** /aggregates/llm — spend / token / invocation rollups. */
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

/** /aggregates/findings — diagnosis hypotheses across experiments. */
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
  /** Bucketed [0,0.2), [0.2,0.4), [0.4,0.6), [0.6,0.8), [0.8,1.0]. */
  confidence_histogram: Array<{ bucket: string; count: number }>;
  recent: Array<{
    experiment_id: string;
    summary: string;
    confidence: number;
    fix_class: SuggestedFixClass;
    started_at: IsoDatetime;
  }>;
}

/** /aggregates/fixes — fix proposal outcomes. */
export interface FixesAggregates {
  totals: {
    fix_proposals: number;
    with_pr: number;
    with_regression_test: number;
    mean_confidence: number;
  };
  by_action: Array<{ action: FixAction; count: number }>;
  /** Top N (20) most frequently touched paths across all proposals. */
  by_file: Array<{ path: string; count: number }>;
  /** YYYY-MM-DD bucket counts of fix-proposal throughput. */
  by_day: Array<{ date: string; count: number }>;
}
