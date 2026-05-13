import { Injectable } from '@nestjs/common';

import type { ExperimentRecord, FixAction, SuggestedFixClass } from '../contracts';
import { SqliteReaderService } from '../store/sqlite-reader.service';

import type {
  FindingsAggregates,
  FixesAggregates,
  LlmAggregates,
} from './aggregates.types';

/**
 * Cross-experiment projections. Each method walks the SQLite store via
 * `SqliteReaderService.listExperiments`, then folds the records into a
 * shape an ECharts component can render directly.
 *
 * Why fold here, not in the SPA: the orchestrator stores its records as
 * JSON blobs (one per row), so the SQL layer can't aggregate. Doing it
 * here means the SPA only ships the aggregate shape (kBs), not every
 * record (MBs as the run history grows).
 */
@Injectable()
export class AggregatesService {
  constructor(private readonly store: SqliteReaderService) {}

  getLlm(opts: WindowOptions = {}): LlmAggregates {
    const records = this.fetch(opts);
    let spend = 0;
    let prompt = 0;
    let completion = 0;
    let invocations = 0;
    let withLlm = 0;
    const agentMap = new Map<string, LlmAggregates['by_agent'][number]>();
    const byExp: LlmAggregates['by_experiment'] = [];

    for (const r of records) {
      let expSpend = 0;
      let expTokens = 0;
      for (const inv of r.agent_invocations) {
        const s = inv.spend_usd ?? 0;
        const p = inv.prompt_tokens ?? 0;
        const c = inv.completion_tokens ?? 0;
        spend += s;
        prompt += p;
        completion += c;
        invocations += 1;
        if (s > 0 || p > 0) withLlm += 1;
        expSpend += s;
        expTokens += p + c;

        const row = agentMap.get(inv.agent) ?? {
          agent: inv.agent,
          spend_usd: 0,
          prompt_tokens: 0,
          completion_tokens: 0,
          invocations: 0,
        };
        row.spend_usd += s;
        row.prompt_tokens += p;
        row.completion_tokens += c;
        row.invocations += 1;
        agentMap.set(inv.agent, row);
      }
      byExp.push({
        experiment_id: r.experiment_id,
        title: r.plan.title,
        started_at: r.started_at,
        spend_usd: expSpend,
        tokens: expTokens,
      });
    }

    return {
      totals: {
        spend_usd: spend,
        prompt_tokens: prompt,
        completion_tokens: completion,
        experiments: records.length,
        invocations,
        invocations_with_llm: withLlm,
      },
      by_agent: [...agentMap.values()].sort(
        (a, b) => b.spend_usd - a.spend_usd || b.invocations - a.invocations,
      ),
      // Newest-first matches the list page.
      by_experiment: byExp.sort(
        (a, b) =>
          new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
      ),
    };
  }

  getFindings(opts: WindowOptions = {}): FindingsAggregates {
    const records = this.fetch(opts);
    let withDiagnosis = 0;
    let total = 0;
    let confSum = 0;
    const classMap = new Map<
      SuggestedFixClass,
      { count: number; confSum: number }
    >();
    const buckets = bucketLabels();
    const histogram = new Map<string, number>(buckets.map((b) => [b, 0]));
    const recent: FindingsAggregates['recent'] = [];

    for (const r of records) {
      const d = r.diagnosis;
      if (!d || d.hypotheses.length === 0) continue;
      withDiagnosis += 1;
      for (const h of d.hypotheses) {
        total += 1;
        confSum += h.confidence;
        const slot = classMap.get(h.suggested_fix_class) ?? {
          count: 0,
          confSum: 0,
        };
        slot.count += 1;
        slot.confSum += h.confidence;
        classMap.set(h.suggested_fix_class, slot);

        const b = bucketFor(h.confidence);
        histogram.set(b, (histogram.get(b) ?? 0) + 1);

        recent.push({
          experiment_id: r.experiment_id,
          summary: h.summary,
          confidence: h.confidence,
          fix_class: h.suggested_fix_class,
          started_at: r.started_at,
        });
      }
    }

    return {
      totals: {
        experiments_with_diagnosis: withDiagnosis,
        total_hypotheses: total,
        mean_confidence: total > 0 ? confSum / total : 0,
      },
      by_fix_class: [...classMap.entries()]
        .map(([fix_class, slot]) => ({
          fix_class,
          count: slot.count,
          mean_confidence: slot.confSum / slot.count,
        }))
        .sort((a, b) => b.count - a.count),
      confidence_histogram: buckets.map((bucket) => ({
        bucket,
        count: histogram.get(bucket) ?? 0,
      })),
      // Top 50 newest hypotheses; the page renders the table + drills in.
      recent: recent
        .sort(
          (a, b) =>
            new Date(b.started_at).getTime() -
            new Date(a.started_at).getTime(),
        )
        .slice(0, 50),
    };
  }

  getFixes(opts: WindowOptions = {}): FixesAggregates {
    const records = this.fetch(opts);
    let proposals = 0;
    let withPr = 0;
    let withRegression = 0;
    let confSum = 0;
    const actionMap = new Map<FixAction, number>();
    const fileMap = new Map<string, number>();
    const dayMap = new Map<string, number>();

    for (const r of records) {
      const f = r.fix_proposal;
      if (!f) continue;
      proposals += 1;
      if (f.pr_url) withPr += 1;
      if (f.regression_test_added) withRegression += 1;
      confSum += f.confidence;

      actionMap.set(f.action, (actionMap.get(f.action) ?? 0) + 1);
      for (const path of f.files_touched) {
        fileMap.set(path, (fileMap.get(path) ?? 0) + 1);
      }

      const day = r.started_at.slice(0, 10); // YYYY-MM-DD prefix of ISO datetime
      dayMap.set(day, (dayMap.get(day) ?? 0) + 1);
    }

    return {
      totals: {
        fix_proposals: proposals,
        with_pr: withPr,
        with_regression_test: withRegression,
        mean_confidence: proposals > 0 ? confSum / proposals : 0,
      },
      by_action: [...actionMap.entries()]
        .map(([action, count]) => ({ action, count }))
        .sort((a, b) => b.count - a.count),
      by_file: [...fileMap.entries()]
        .map(([path, count]) => ({ path, count }))
        .sort((a, b) => b.count - a.count)
        .slice(0, 20),
      by_day: [...dayMap.entries()]
        .map(([date, count]) => ({ date, count }))
        .sort((a, b) => a.date.localeCompare(b.date)),
    };
  }

  private fetch(opts: WindowOptions): ExperimentRecord[] {
    // Stream all matching records through SQLite's 500-row page boundary.
    // `listExperiments` is HTTP-bounded by design; aggregates need every row.
    const records: ExperimentRecord[] = [];
    this.store.forEachExperiment(opts, (r) => records.push(r));
    return records;
  }
}

export interface WindowOptions {
  from?: string;
  to?: string;
}

function bucketLabels(): string[] {
  return ['0.0–0.2', '0.2–0.4', '0.4–0.6', '0.6–0.8', '0.8–1.0'];
}

function bucketFor(confidence: number): string {
  // Inclusive on the upper bound for the last bucket so 1.0 lands in 0.8–1.0.
  if (confidence < 0.2) return '0.0–0.2';
  if (confidence < 0.4) return '0.2–0.4';
  if (confidence < 0.6) return '0.4–0.6';
  if (confidence < 0.8) return '0.6–0.8';
  return '0.8–1.0';
}
