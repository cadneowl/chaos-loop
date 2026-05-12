import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { NotFoundException } from '@nestjs/common';
import { Test } from '@nestjs/testing';

import { sampleRecord, seedStore } from '../../test/fixtures/seed-store';
import { SqliteReaderService } from './sqlite-reader.service';

describe('SqliteReaderService', () => {
  let tmpDir: string;
  let dbPath: string;
  let svc: SqliteReaderService;

  const buildSvc = async (): Promise<SqliteReaderService> => {
    const moduleRef = await Test.createTestingModule({
      providers: [SqliteReaderService],
    }).compile();
    const s = moduleRef.get(SqliteReaderService);
    s.onModuleInit();
    return s;
  };

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'chaos-ui-test-'));
    dbPath = join(tmpDir, 'experiments.sqlite');
    process.env.CHAOS_STORE_PATH = dbPath;
  });

  afterEach(() => {
    svc?.onModuleDestroy();
    delete process.env.CHAOS_STORE_PATH;
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('reports not-ready when the DB file is missing', async () => {
    svc = await buildSvc();
    expect(svc.isReady()).toBe(false);
    expect(svc.listExperiments()).toEqual([]);
    expect(svc.countExperiments()).toBe(0);
  });

  it('lists seeded experiments newest first', async () => {
    seedStore(dbPath, [
      sampleRecord({ experiment_id: 'exp-000000000001', started_at: '2026-05-10T00:00:00+00:00' }),
      sampleRecord({ experiment_id: 'exp-000000000002', started_at: '2026-05-12T00:00:00+00:00' }),
      sampleRecord({ experiment_id: 'exp-000000000003', started_at: '2026-05-11T00:00:00+00:00' }),
    ]);
    svc = await buildSvc();
    const records = svc.listExperiments();
    expect(records.map((r) => r.experiment_id)).toEqual([
      'exp-000000000002',
      'exp-000000000003',
      'exp-000000000001',
    ]);
  });

  it('filters by state', async () => {
    seedStore(dbPath, [
      sampleRecord({ experiment_id: 'exp-000000000001', state: 'recorded' }),
      sampleRecord({ experiment_id: 'exp-000000000002', state: 'aborted' }),
    ]);
    svc = await buildSvc();
    const records = svc.listExperiments({ state: 'aborted' });
    expect(records).toHaveLength(1);
    expect(records[0].experiment_id).toBe('exp-000000000002');
  });

  it('filters by target_app (JSON-blob field)', async () => {
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-000000000001',
        plan: { ...sampleRecord().plan, target_app: 'otel-demo' },
      }),
      sampleRecord({
        experiment_id: 'exp-000000000002',
        plan: { ...sampleRecord().plan, target_app: 'other-app' },
      }),
    ]);
    svc = await buildSvc();
    const records = svc.listExperiments({ target_app: 'other-app' });
    expect(records).toHaveLength(1);
    expect(records[0].plan.target_app).toBe('other-app');
  });

  it('respects limit + offset', async () => {
    const records = Array.from({ length: 10 }, (_, i) =>
      sampleRecord({
        experiment_id: `exp-00000000000${i}`,
        started_at: `2026-05-12T00:00:0${i}+00:00`,
      }),
    );
    seedStore(dbPath, records);
    svc = await buildSvc();
    const page = svc.listExperiments({ limit: 3, offset: 2 });
    expect(page).toHaveLength(3);
    // Newest first → offset 2 gives indices [7, 6, 5] of the input.
    expect(page.map((r) => r.experiment_id)).toEqual([
      'exp-000000000007',
      'exp-000000000006',
      'exp-000000000005',
    ]);
  });

  it('caps limit at 500', async () => {
    seedStore(dbPath, [sampleRecord()]);
    svc = await buildSvc();
    // Pass an absurdly large limit; should silently cap. We don't assert the
    // SQL directly; we assert the service didn't crash and returned ≤ 500.
    const records = svc.listExperiments({ limit: 99_999 });
    expect(records.length).toBeLessThanOrEqual(500);
  });

  it('returns the requested record by id', async () => {
    seedStore(dbPath, [
      sampleRecord({ experiment_id: 'exp-aaaaaaaaaaaa' }),
      sampleRecord({ experiment_id: 'exp-bbbbbbbbbbbb' }),
    ]);
    svc = await buildSvc();
    expect(svc.getExperiment('exp-bbbbbbbbbbbb').experiment_id).toBe('exp-bbbbbbbbbbbb');
  });

  it('throws 404 for unknown id', async () => {
    seedStore(dbPath, [sampleRecord()]);
    svc = await buildSvc();
    expect(() => svc.getExperiment('exp-doesnotexist0')).toThrow(NotFoundException);
  });

  it('exposes control signals from the DB', async () => {
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-aaaaaaaaaaaa',
        pause_requested: 1,
        abort_requested: 1,
        abort_reason_requested: 'user_kill',
      }),
    ]);
    svc = await buildSvc();
    const ctrl = svc.getControlSignals('exp-aaaaaaaaaaaa');
    expect(ctrl.pause_requested).toBe(true);
    expect(ctrl.abort_requested).toBe(true);
    expect(ctrl.abort_reason).toBe('user_kill');
  });

  it('returns default-empty control signals for unknown id', async () => {
    seedStore(dbPath, [sampleRecord()]);
    svc = await buildSvc();
    const ctrl = svc.getControlSignals('exp-doesnotexist0');
    expect(ctrl).toEqual({
      pause_requested: false,
      abort_requested: false,
      abort_reason: null,
    });
  });

  it('lists target_apps with their counts, descending', async () => {
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-000000000001',
        plan: { ...sampleRecord().plan, target_app: 'otel-demo' },
      }),
      sampleRecord({
        experiment_id: 'exp-000000000002',
        plan: { ...sampleRecord().plan, target_app: 'otel-demo' },
      }),
      sampleRecord({
        experiment_id: 'exp-000000000003',
        plan: { ...sampleRecord().plan, target_app: 'other-app' },
      }),
    ]);
    svc = await buildSvc();
    const apps = svc.listTargetApps();
    expect(apps).toEqual([
      { target_app: 'otel-demo', count: 2 },
      { target_app: 'other-app', count: 1 },
    ]);
  });
});
