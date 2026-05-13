import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { Test } from '@nestjs/testing';

import type { AgentInvocationLog, ExperimentRecord } from '../contracts';
import { sampleRecord, seedStore } from '../../test/fixtures/seed-store';
import { SqliteReaderService } from '../store/sqlite-reader.service';

import { AggregatesService } from './aggregates.service';

function inv(o: Partial<AgentInvocationLog> = {}): AgentInvocationLog {
  return {
    agent: 'tester',
    method: 'baseline',
    started_at_ms: 0,
    ok: true,
    input_summary: '',
    output_summary: '',
    tool_calls: [],
    ...o,
  };
}

describe('AggregatesService', () => {
  let tmpDir: string;
  let dbPath: string;
  let reader: SqliteReaderService;
  let svc: AggregatesService;

  const buildSvc = async (): Promise<void> => {
    const moduleRef = await Test.createTestingModule({
      providers: [SqliteReaderService, AggregatesService],
    }).compile();
    reader = moduleRef.get(SqliteReaderService);
    reader.onModuleInit();
    svc = moduleRef.get(AggregatesService);
  };

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'chaos-aggregates-'));
    dbPath = join(tmpDir, 'experiments.sqlite');
    process.env.CHAOS_STORE_PATH = dbPath;
  });

  afterEach(() => {
    reader?.onModuleDestroy();
    delete process.env.CHAOS_STORE_PATH;
    rmSync(tmpDir, { recursive: true, force: true });
  });

  // ---------------------------- LLM ----------------------------

  it('LLM aggregates: sums spend + tokens, groups by agent, sorts by-experiment newest first', async () => {
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-000000000001',
        started_at: '2026-05-10T00:00:00+00:00',
        agent_invocations: [
          inv({ agent: 'tester', spend_usd: 0.10, prompt_tokens: 100, completion_tokens: 50 }),
          inv({ agent: 'fixer', spend_usd: 0.05, prompt_tokens: 200, completion_tokens: 100 }),
        ],
      }),
      sampleRecord({
        experiment_id: 'exp-000000000002',
        started_at: '2026-05-12T00:00:00+00:00',
        agent_invocations: [
          inv({ agent: 'tester', spend_usd: 0.20, prompt_tokens: 300, completion_tokens: 150 }),
        ],
      }),
    ]);
    await buildSvc();

    const out = svc.getLlm();
    expect(out.totals.spend_usd).toBeCloseTo(0.35, 6);
    expect(out.totals).toMatchObject({
      prompt_tokens: 600,
      completion_tokens: 300,
      experiments: 2,
      invocations: 3,
      invocations_with_llm: 3,
    });
    expect(out.by_agent.map((a) => a.agent)).toEqual(['tester', 'fixer']);
    expect(out.by_agent[0].agent).toBe('tester');
    expect(out.by_agent[0].spend_usd).toBeCloseTo(0.30, 6);
    expect(out.by_agent[0]).toMatchObject({
      agent: 'tester',
      prompt_tokens: 400,
      completion_tokens: 200,
      invocations: 2,
    });
    expect(out.by_experiment.map((e) => e.experiment_id)).toEqual([
      'exp-000000000002',
      'exp-000000000001',
    ]);
    expect(out.by_experiment[1].spend_usd).toBeCloseTo(0.15, 6);
    expect(out.by_experiment[1]).toMatchObject({
      experiment_id: 'exp-000000000001',
      title: 'sample run',
      started_at: '2026-05-10T00:00:00+00:00',
      tokens: 450,
    });
  });

  it('LLM aggregates: invocations_with_llm only counts spend>0 or tokens>0', async () => {
    seedStore(dbPath, [
      sampleRecord({
        agent_invocations: [
          inv({ agent: 'chaos', spend_usd: 0, prompt_tokens: 0 }),
          inv({ agent: 'tester', spend_usd: 0, prompt_tokens: 100 }),
          inv({ agent: 'fixer', spend_usd: 0.5, prompt_tokens: 0 }),
        ],
      }),
    ]);
    await buildSvc();
    expect(svc.getLlm().totals.invocations_with_llm).toBe(2);
  });

  // ---------------------------- Findings ----------------------------

  it('Findings aggregates: groups by fix_class, computes mean confidence, buckets correctly', async () => {
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-000000000001',
        diagnosis: {
          experiment_id: 'exp-000000000001',
          run_id: 'run-1',
          notes: '',
          started_at: '2026-05-12T10:00:00+00:00',
          finished_at: null,
          hypotheses: [
            { summary: 'a', confidence: 0.85, evidence: [], suggested_fix_class: 'missing-retry', affected_paths: [] },
            { summary: 'b', confidence: 0.65, evidence: [], suggested_fix_class: 'missing-retry', affected_paths: [] },
            { summary: 'c', confidence: 0.30, evidence: [], suggested_fix_class: 'missing-circuit-breaker', affected_paths: [] },
          ],
        },
      }),
    ]);
    await buildSvc();

    const out = svc.getFindings();
    expect(out.totals).toEqual({
      experiments_with_diagnosis: 1,
      total_hypotheses: 3,
      mean_confidence: (0.85 + 0.65 + 0.30) / 3,
    });
    const retry = out.by_fix_class.find((c) => c.fix_class === 'missing-retry');
    expect(retry).toEqual({
      fix_class: 'missing-retry',
      count: 2,
      mean_confidence: 0.75,
    });
    expect(out.confidence_histogram.find((b) => b.bucket === '0.8–1.0')?.count).toBe(1);
    expect(out.confidence_histogram.find((b) => b.bucket === '0.6–0.8')?.count).toBe(1);
    expect(out.confidence_histogram.find((b) => b.bucket === '0.2–0.4')?.count).toBe(1);
  });

  it('Findings aggregates: empty store yields zeroes (no NaN division)', async () => {
    await buildSvc();
    expect(svc.getFindings().totals.mean_confidence).toBe(0);
  });

  // ---------------------------- Fixes ----------------------------

  it('Fixes aggregates: counts actions, top files, daily throughput', async () => {
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-000000000001',
        started_at: '2026-05-10T08:00:00+00:00',
        fix_proposal: {
          experiment_id: 'exp-000000000001',
          run_id: 'run-1',
          action: 'code-patch',
          pr_url: 'https://example.invalid/pr/1',
          confidence: 0.8,
          reasoning: '',
          files_touched: ['services/cart/redis_client.py', 'tests/test_redis.py'],
          regression_test_added: true,
          is_draft: true,
          started_at: '2026-05-10T08:00:00+00:00',
          finished_at: null,
        },
      }),
      sampleRecord({
        experiment_id: 'exp-000000000002',
        started_at: '2026-05-12T09:00:00+00:00',
        fix_proposal: {
          experiment_id: 'exp-000000000002',
          run_id: 'run-2',
          action: 'code-patch',
          pr_url: null,
          confidence: 0.4,
          reasoning: '',
          files_touched: ['services/cart/redis_client.py'],
          regression_test_added: false,
          is_draft: true,
          started_at: '2026-05-12T09:00:00+00:00',
          finished_at: null,
        },
      }),
    ]);
    await buildSvc();

    const out = svc.getFixes();
    expect(out.totals.mean_confidence).toBeCloseTo(0.6, 6);
    expect(out.totals).toMatchObject({
      fix_proposals: 2,
      with_pr: 1,
      with_regression_test: 1,
    });
    expect(out.by_action).toEqual([{ action: 'code-patch', count: 2 }]);
    expect(out.by_file[0]).toEqual({ path: 'services/cart/redis_client.py', count: 2 });
    expect(out.by_day).toEqual([
      { date: '2026-05-10', count: 1 },
      { date: '2026-05-12', count: 1 },
    ]);
  });

  it('streams past SQLite\'s 500-row page boundary (regression: silent truncation)', async () => {
    // Seed 600 records; `listExperiments` clamps at 500, so a naive
    // `listExperiments({ limit: 10000 })` would have silently dropped 100.
    // `forEachExperiment` pages through all of them.
    const records = Array.from({ length: 600 }, (_, i) =>
      sampleRecord({
        experiment_id: `exp-${i.toString(16).padStart(12, '0')}`,
        started_at: new Date(2026, 0, 1, 0, i).toISOString(),
        agent_invocations: [inv({ spend_usd: 0.01 })],
      }),
    );
    seedStore(dbPath, records);
    await buildSvc();

    const out = svc.getLlm();
    expect(out.totals.experiments).toBe(600);
    expect(out.totals.invocations).toBe(600);
    expect(out.totals.spend_usd).toBeCloseTo(6.0, 4);
    expect(out.by_experiment).toHaveLength(600);
  });

  it('window: from/to scopes the aggregate', async () => {
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-000000000001',
        started_at: '2026-05-01T00:00:00+00:00',
        agent_invocations: [inv({ spend_usd: 1.0 })],
      }),
      sampleRecord({
        experiment_id: 'exp-000000000002',
        started_at: '2026-05-15T00:00:00+00:00',
        agent_invocations: [inv({ spend_usd: 2.0 })],
      }),
    ]);
    await buildSvc();
    const recent = svc.getLlm({ from: '2026-05-10T00:00:00+00:00' });
    expect(recent.totals.spend_usd).toBe(2.0);
    expect(recent.by_experiment).toHaveLength(1);
  });
});
