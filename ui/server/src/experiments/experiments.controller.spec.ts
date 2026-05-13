import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { NotFoundException } from '@nestjs/common';
import { Test } from '@nestjs/testing';

import { sampleRecord, seedStore } from '../../test/fixtures/seed-store';
import { SqliteReaderService } from '../store/sqlite-reader.service';
import { SqliteWriterService } from '../store/sqlite-writer.service';
import { ExperimentsController } from './experiments.controller';

describe('ExperimentsController', () => {
  let tmpDir: string;
  let dbPath: string;
  let controller: ExperimentsController;
  let store: SqliteReaderService;
  let writer: SqliteWriterService;

  beforeEach(async () => {
    tmpDir = mkdtempSync(join(tmpdir(), 'chaos-ui-ec-'));
    dbPath = join(tmpDir, 'experiments.sqlite');
    process.env.CHAOS_STORE_PATH = dbPath;
    seedStore(dbPath, [
      sampleRecord({
        experiment_id: 'exp-aaaaaaaaaaaa',
        started_at: '2026-05-12T10:00:00+00:00',
        finished_at: '2026-05-12T10:05:00+00:00',
      }),
      sampleRecord({
        experiment_id: 'exp-bbbbbbbbbbbb',
        started_at: '2026-05-12T11:00:00+00:00',
        state: 'aborted',
        abort_reason: 'user_kill',
      }),
    ]);
    const moduleRef = await Test.createTestingModule({
      controllers: [ExperimentsController],
      providers: [SqliteReaderService, SqliteWriterService],
    }).compile();
    store = moduleRef.get(SqliteReaderService);
    store.onModuleInit();
    writer = moduleRef.get(SqliteWriterService);
    writer.onModuleInit();
    controller = moduleRef.get(ExperimentsController);
  });

  afterEach(() => {
    writer?.onModuleDestroy();
    store.onModuleDestroy();
    delete process.env.CHAOS_STORE_PATH;
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('list — newest first, paginated, with totals', () => {
    const response = controller.list();
    expect(response.total).toBe(2);
    expect(response.results).toHaveLength(2);
    expect(response.results[0].experiment_id).toBe('exp-bbbbbbbbbbbb');
  });

  it('list — summary computes duration_seconds from finished_at - started_at', () => {
    const response = controller.list();
    const finished = response.results.find((r) => r.experiment_id === 'exp-aaaaaaaaaaaa')!;
    expect(finished.duration_seconds).toBe(300);
  });

  it('list — paused records report is_paused=true', () => {
    // Close both the reader and the writer before mutating the file (Windows
    // holds an exclusive handle until every SQLite connection is closed),
    // then reseed and reopen both.
    store.onModuleDestroy();
    writer.onModuleDestroy();
    rmSync(dbPath, { force: true });
    seedStore(dbPath, [sampleRecord({ experiment_id: 'exp-paused000000', state: 'paused' })]);
    store.onModuleInit();
    writer.onModuleInit();
    const response = controller.list();
    expect(response.results[0].is_paused).toBe(true);
  });

  it('list — state filter passes through to the store', () => {
    const response = controller.list('aborted');
    expect(response.results).toHaveLength(1);
    expect(response.results[0].experiment_id).toBe('exp-bbbbbbbbbbbb');
    expect(response.total).toBe(1);
  });

  it('detail — returns the full record', () => {
    const record = controller.detail('exp-aaaaaaaaaaaa');
    expect(record.experiment_id).toBe('exp-aaaaaaaaaaaa');
    expect(record.plan.target_app).toBe('otel-demo');
  });

  it('detail — throws 404 for unknown id', () => {
    expect(() => controller.detail('exp-doesnotexist0')).toThrow(NotFoundException);
  });

  it('control — returns default empty for unknown id', () => {
    const ctrl = controller.control('exp-doesnotexist0');
    expect(ctrl).toEqual({
      pause_requested: false,
      abort_requested: false,
      abort_reason: null,
    });
  });

  it('pause — flips pause_requested and reflects in /control', () => {
    controller.pause('exp-aaaaaaaaaaaa');
    expect(controller.control('exp-aaaaaaaaaaaa').pause_requested).toBe(true);
  });

  it('resume — clears pause_requested', () => {
    controller.pause('exp-aaaaaaaaaaaa');
    controller.resume('exp-aaaaaaaaaaaa');
    expect(controller.control('exp-aaaaaaaaaaaa').pause_requested).toBe(false);
  });

  it('abort — sets the flag with a default user_kill reason', () => {
    controller.abort('exp-aaaaaaaaaaaa');
    const c = controller.control('exp-aaaaaaaaaaaa');
    expect(c.abort_requested).toBe(true);
    expect(c.abort_reason).toBe('user_kill');
  });

  it('abort — accepts an explicit reason', () => {
    controller.abort('exp-aaaaaaaaaaaa', { reason: 'slo_breach' });
    expect(controller.control('exp-aaaaaaaaaaaa').abort_reason).toBe('slo_breach');
  });

  it('abort — rejects an invalid reason', () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(() => controller.abort('exp-aaaaaaaaaaaa', { reason: 'rage_quit' as any })).toThrow(
      /invalid abort reason/i,
    );
  });

  it('pause / abort throw 404 for unknown experiment', () => {
    expect(() => controller.pause('exp-doesnotexist0')).toThrow(NotFoundException);
    expect(() => controller.abort('exp-doesnotexist0')).toThrow(NotFoundException);
  });
});
