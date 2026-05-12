/**
 * Test-only helpers — building an experiments.sqlite that mirrors the Python
 * orchestrator's schema + sample data.
 *
 * Lives under `src/` (not `test/`) so the same ts-jest transformer + tsconfig
 * picks it up. Marked `@internal` so consumers grep loudly if they import it
 * outside of *.spec.ts files.
 *
 * @internal
 */

import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import type { ExperimentRecord } from '../../src/contracts';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    abort_reason TEXT,
    spend_usd REAL NOT NULL DEFAULT 0,
    blob TEXT NOT NULL,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    abort_requested INTEGER NOT NULL DEFAULT 0,
    abort_reason_requested TEXT
);
CREATE INDEX IF NOT EXISTS idx_state ON experiments(state);
CREATE INDEX IF NOT EXISTS idx_started_at ON experiments(started_at);
`;

/** Per-row control-flag overrides not visible on the contract. */
export interface SeedRecord extends ExperimentRecord {
  pause_requested?: number;
  abort_requested?: number;
  abort_reason_requested?: string | null;
}

export function sampleRecord(overrides: Partial<SeedRecord> = {}): SeedRecord {
  const base: SeedRecord = {
    experiment_id: 'exp-aaaaaaaaaaaa',
    state: 'recorded',
    started_at: '2026-05-12T10:00:00.000000+00:00',
    finished_at: '2026-05-12T10:05:00.000000+00:00',
    abort_reason: null,
    abort_detail: '',
    spend_usd: 0.42,
    plan: {
      experiment_id: 'exp-aaaaaaaaaaaa',
      title: 'sample run',
      target_app: 'otel-demo',
      target_repo: null,
      faults: [
        {
          category: 'network',
          name: 'network.loss',
          target_selector: { 'app.kubernetes.io/component': 'valkey-cart' },
          parameters: { loss_percent: 100 },
          duration_seconds: 60,
          requires_approval: false,
          rationale: 'sample',
          hypothesis_id: null,
        },
      ],
      safety: {
        cluster_context: 'kind-chaos-dev',
        namespace: 'otel-demo',
        max_pods_affected: 1,
        max_duration_seconds: 120,
        allow_multi_fault: false,
        require_namespace_annotation: false,
        forbidden_cluster_substrings: ['prod', 'production', 'live', 'main'],
      },
      budget: { soft_cap_usd: 1.0, hard_cap_usd: 5.0, wall_clock_seconds: 900 },
      quiet_window_pre_seconds: 60,
      quiet_window_post_seconds: 60,
      created_at: '2026-05-12T10:00:00.000000+00:00',
    },
    tester_baseline: null,
    security_baseline: null,
    chaos_timeline: null,
    tester_verify: null,
    security_verify: null,
    diagnosis: null,
    fix_proposal: null,
    agent_invocations: [],
  };
  return { ...base, ...overrides };
}

export function seedStore(path: string, records: SeedRecord[]): void {
  mkdirSync(dirname(path), { recursive: true });
  const db = new Database(path);
  db.exec(SCHEMA);
  const insert = db.prepare(
    `INSERT INTO experiments
       (experiment_id, state, started_at, finished_at, abort_reason, spend_usd, blob,
        pause_requested, abort_requested, abort_reason_requested)
     VALUES
       (@experiment_id, @state, @started_at, @finished_at, @abort_reason, @spend_usd, @blob,
        @pause_requested, @abort_requested, @abort_reason_requested)`,
  );
  for (const r of records) {
    insert.run({
      experiment_id: r.experiment_id,
      state: r.state,
      started_at: r.started_at,
      finished_at: r.finished_at ?? null,
      abort_reason: r.abort_reason ?? null,
      spend_usd: r.spend_usd,
      blob: JSON.stringify(r),
      pause_requested: r.pause_requested ?? 0,
      abort_requested: r.abort_requested ?? 0,
      abort_reason_requested: r.abort_reason_requested ?? null,
    });
  }
  db.close();
}
