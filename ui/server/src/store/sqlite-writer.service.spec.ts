import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { NotFoundException } from '@nestjs/common';
import Database from 'better-sqlite3';
import { Test } from '@nestjs/testing';

import { sampleRecord, seedStore } from '../../test/fixtures/seed-store';
import { SqliteWriterService } from './sqlite-writer.service';

describe('SqliteWriterService', () => {
  let tmpDir: string;
  let dbPath: string;
  let svc: SqliteWriterService;

  const buildSvc = async (): Promise<void> => {
    const moduleRef = await Test.createTestingModule({
      providers: [SqliteWriterService],
    }).compile();
    svc = moduleRef.get(SqliteWriterService);
    svc.onModuleInit();
  };

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'chaos-writer-'));
    dbPath = join(tmpDir, 'experiments.sqlite');
    process.env.CHAOS_STORE_PATH = dbPath;
  });

  afterEach(() => {
    svc?.onModuleDestroy();
    delete process.env.CHAOS_STORE_PATH;
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('setPause flips the pause_requested column', async () => {
    seedStore(dbPath, [sampleRecord({ experiment_id: 'exp-aaaaaaaaaaaa' })]);
    await buildSvc();
    svc.setPause('exp-aaaaaaaaaaaa', true);
    const db = new Database(dbPath, { readonly: true });
    const row = db
      .prepare('SELECT pause_requested FROM experiments WHERE experiment_id = ?')
      .get('exp-aaaaaaaaaaaa') as { pause_requested: number };
    expect(row.pause_requested).toBe(1);
    svc.setPause('exp-aaaaaaaaaaaa', false);
    const row2 = db
      .prepare('SELECT pause_requested FROM experiments WHERE experiment_id = ?')
      .get('exp-aaaaaaaaaaaa') as { pause_requested: number };
    expect(row2.pause_requested).toBe(0);
    db.close();
  });

  it('requestAbort sets both the flag and the reason', async () => {
    seedStore(dbPath, [sampleRecord({ experiment_id: 'exp-aaaaaaaaaaaa' })]);
    await buildSvc();
    svc.requestAbort('exp-aaaaaaaaaaaa', 'user_kill');
    const db = new Database(dbPath, { readonly: true });
    const row = db
      .prepare(
        'SELECT abort_requested, abort_reason_requested FROM experiments WHERE experiment_id = ?',
      )
      .get('exp-aaaaaaaaaaaa') as {
      abort_requested: number;
      abort_reason_requested: string;
    };
    expect(row.abort_requested).toBe(1);
    expect(row.abort_reason_requested).toBe('user_kill');
    db.close();
  });

  it('throws 404 when the experiment does not exist', async () => {
    seedStore(dbPath, []);
    await buildSvc();
    expect(() => svc.setPause('exp-doesnotexist', true)).toThrow(NotFoundException);
    expect(() => svc.requestAbort('exp-doesnotexist', 'user_kill')).toThrow(
      NotFoundException,
    );
  });
});
