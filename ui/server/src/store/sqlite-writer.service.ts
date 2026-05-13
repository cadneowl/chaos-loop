import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

import {
  Injectable,
  InternalServerErrorException,
  Logger,
  NotFoundException,
  OnModuleDestroy,
  OnModuleInit,
} from '@nestjs/common';
import Database, { type Database as DB } from 'better-sqlite3';

import type { AbortReason } from '../contracts';

/**
 * Writer connection for control signals on existing experiment rows.
 *
 * This is the **only** write surface the UI server has. It targets three
 * columns the Python orchestrator polls between state transitions:
 *
 *   `pause_requested`         INTEGER (0/1)
 *   `abort_requested`         INTEGER (0/1)
 *   `abort_reason_requested`  TEXT (an AbortReason value)
 *
 * Updates never touch the audit blob, state, or budget — those are the
 * orchestrator's domain. Failing safely matters here: a wrong reading of
 * a control flag could either freeze an experiment forever (pause stuck on)
 * or kill it prematurely (spurious abort). Use prepared statements with
 * parameter binding and bail loudly on any unexpected row count.
 */
@Injectable()
export class SqliteWriterService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(SqliteWriterService.name);
  private db: DB | null = null;
  public readonly storePath: string;

  constructor() {
    this.storePath =
      process.env.CHAOS_STORE_PATH ??
      join(homedir(), '.local', 'share', 'chaos', 'experiments.sqlite');
  }

  onModuleInit(): void {
    if (!existsSync(this.storePath)) {
      this.logger.warn(
        `SQLite store not found at ${this.storePath} — control endpoints will 404. ` +
          'The Python orchestrator creates the file on its first run.',
      );
      return;
    }
    try {
      // Writable connection — Python is still the primary writer; we only
      // touch the three control-signal columns. WAL mode is owned by the
      // orchestrator's first writer connection.
      this.db = new Database(this.storePath, { fileMustExist: true });
      this.db.pragma('busy_timeout = 5000');
      this.logger.log(`opened store at ${this.storePath} (writable, control-only)`);
    } catch (err) {
      this.logger.error(`failed to open ${this.storePath}: ${(err as Error).message}`);
      throw new InternalServerErrorException(
        `cannot open chaos store at ${this.storePath}`,
      );
    }
  }

  onModuleDestroy(): void {
    this.db?.close();
    this.db = null;
  }

  /** Set or clear the pause flag. Throws 404 if the experiment doesn't exist. */
  setPause(experimentId: string, paused: boolean): void {
    if (!this.db) throw new NotFoundException(`no experiment ${experimentId}`);
    const result = this.db
      .prepare(
        'UPDATE experiments SET pause_requested = ? WHERE experiment_id = ?',
      )
      .run(paused ? 1 : 0, experimentId);
    if (result.changes === 0) {
      throw new NotFoundException(`no experiment ${experimentId}`);
    }
  }

  /**
   * Request an abort with the given reason. Idempotent — the orchestrator
   * acts on the first read after the flag is set, then transitions to
   * ABORTED at the next state boundary. 404 if no such experiment.
   */
  requestAbort(experimentId: string, reason: AbortReason): void {
    if (!this.db) throw new NotFoundException(`no experiment ${experimentId}`);
    const result = this.db
      .prepare(
        'UPDATE experiments ' +
          'SET abort_requested = 1, abort_reason_requested = ? ' +
          'WHERE experiment_id = ?',
      )
      .run(reason, experimentId);
    if (result.changes === 0) {
      throw new NotFoundException(`no experiment ${experimentId}`);
    }
  }
}
