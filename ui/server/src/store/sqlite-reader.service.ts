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

import type { ControlSignals, ExperimentRecord, ExperimentState } from '../contracts';

/**
 * Read-only access to the orchestrator's SQLite store.
 *
 * Single-writer (Python orchestrator), multi-reader (this server, plus
 * any other tooling). better-sqlite3 in `readonly: true` mode + WAL on
 * the writer's side gives us snapshot reads that never block writes.
 *
 * The store path is resolved at construction time:
 *   1. `CHAOS_STORE_PATH` env var if set
 *   2. `~/.local/share/chaos/experiments.sqlite` (orchestrator default)
 *
 * If the file doesn't exist (orchestrator never ran), the service still
 * boots — every read returns "no records" rather than crashing the API.
 */
@Injectable()
export class SqliteReaderService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(SqliteReaderService.name);
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
        `SQLite store not found at ${this.storePath} — every read will return empty. ` +
          'The Python orchestrator creates the file on its first run.',
      );
      return;
    }
    try {
      this.db = new Database(this.storePath, { readonly: true, fileMustExist: true });
      // WAL mode is owned by the writer; we just consume the snapshot.
      // ``busy_timeout`` makes our reads tolerate a brief writer-held lock
      // during a WAL checkpoint instead of throwing SQLITE_BUSY.
      this.db.pragma('busy_timeout = 5000');
      this.logger.log(`opened store at ${this.storePath} (read-only)`);
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

  /** True iff the store file exists and is open. */
  isReady(): boolean {
    return this.db !== null;
  }

  /**
   * Stream every experiment that matches the filter through a callback,
   * pulling pages of 500 from SQLite. Use this for cross-experiment
   * aggregation — `listExperiments` is page-bounded for HTTP responses.
   */
  forEachExperiment(
    opts: ListOptions,
    visit: (r: ExperimentRecord) => void,
  ): void {
    if (!this.db) return;
    const page = 500;
    let offset = 0;
    while (true) {
      const batch = this.listExperiments({ ...opts, limit: page, offset });
      for (const r of batch) visit(r);
      if (batch.length < page) return;
      offset += page;
    }
  }

  /**
   * Page through experiments, newest first. Filters compose with AND.
   * Limit is capped at 500 to keep one-page responses bounded.
   */
  listExperiments(opts: ListOptions = {}): ExperimentRecord[] {
    if (!this.db) return [];
    const where: string[] = [];
    const params: Record<string, string | number> = {};
    if (opts.state) {
      where.push('state = @state');
      params.state = opts.state;
    }
    if (opts.from) {
      where.push('started_at >= @from');
      params.from = opts.from;
    }
    if (opts.to) {
      where.push('started_at < @to');
      params.to = opts.to;
    }
    const whereClause = where.length ? `WHERE ${where.join(' AND ')}` : '';
    const limit = Math.min(Math.max(opts.limit ?? 50, 1), 500);
    const offset = Math.max(opts.offset ?? 0, 0);
    const sql = `
      SELECT blob
      FROM experiments
      ${whereClause}
      ORDER BY started_at DESC
      LIMIT @limit OFFSET @offset
    `;
    const rows = this.db
      .prepare(sql)
      .all({ ...params, limit, offset }) as Array<{ blob: string }>;
    // The Python side may apply a target_app filter that's nested inside the
    // blob JSON; we apply it here in JS rather than push a custom JSON path
    // into SQL (works the same on every SQLite version, no surprises).
    let records = rows.map((row) => parseRecord(row.blob));
    if (opts.target_app) {
      records = records.filter((r) => r.plan.target_app === opts.target_app);
    }
    return records;
  }

  /** Total experiment count for the same filter set; used to render pagination. */
  countExperiments(opts: ListOptions = {}): number {
    if (!this.db) return 0;
    if (opts.target_app) {
      // target_app lives inside the JSON blob, so we have to walk the rows
      // matching the SQL-side filters. Pages of 500 keep memory bounded;
      // accumulate until we see a short page (the last one).
      const batch = 500;
      let total = 0;
      let offset = 0;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const page = this.listExperiments({ ...opts, limit: batch, offset });
        total += page.length;
        if (page.length < batch) return total;
        offset += batch;
      }
    }
    const where: string[] = [];
    const params: Record<string, string | number> = {};
    if (opts.state) {
      where.push('state = @state');
      params.state = opts.state;
    }
    if (opts.from) {
      where.push('started_at >= @from');
      params.from = opts.from;
    }
    if (opts.to) {
      where.push('started_at < @to');
      params.to = opts.to;
    }
    const sql = `SELECT COUNT(*) AS n FROM experiments ${
      where.length ? `WHERE ${where.join(' AND ')}` : ''
    }`;
    const row = this.db.prepare(sql).get(params) as { n: number };
    return row.n;
  }

  /** Return the full record for one experiment, or throw 404. */
  getExperiment(id: string): ExperimentRecord {
    if (!this.db) {
      throw new NotFoundException(`store not initialized; no experiment ${id}`);
    }
    const row = this.db
      .prepare('SELECT blob FROM experiments WHERE experiment_id = ?')
      .get(id) as { blob: string } | undefined;
    if (!row) {
      throw new NotFoundException(`no experiment ${id}`);
    }
    return parseRecord(row.blob);
  }

  /** Live operator signals on a single row. */
  getControlSignals(id: string): ControlSignals {
    const empty: ControlSignals = {
      pause_requested: false,
      abort_requested: false,
      abort_reason: null,
    };
    if (!this.db) return empty;
    const row = this.db
      .prepare(
        'SELECT pause_requested, abort_requested, abort_reason_requested ' +
          'FROM experiments WHERE experiment_id = ?',
      )
      .get(id) as
      | {
          pause_requested: number;
          abort_requested: number;
          abort_reason_requested: string | null;
        }
      | undefined;
    if (!row) return empty;
    return {
      pause_requested: row.pause_requested === 1,
      abort_requested: row.abort_requested === 1,
      abort_reason: (row.abort_reason_requested ?? null) as ControlSignals['abort_reason'],
    };
  }

  /** Distinct target_apps + their experiment counts. Used by the dashboard. */
  listTargetApps(): Array<{ target_app: string; count: number }> {
    if (!this.db) return [];
    // target_app is inside the JSON blob; walk recent records and tally.
    // For our scale this is fine — see countExperiments for the same trade-off.
    const rows = this.db.prepare('SELECT blob FROM experiments').all() as Array<{
      blob: string;
    }>;
    const counts = new Map<string, number>();
    for (const row of rows) {
      const r = parseRecord(row.blob);
      counts.set(r.plan.target_app, (counts.get(r.plan.target_app) ?? 0) + 1);
    }
    return [...counts.entries()]
      .map(([target_app, count]) => ({ target_app, count }))
      .sort((a, b) => b.count - a.count);
  }
}

export interface ListOptions {
  state?: ExperimentState;
  target_app?: string;
  /** ISO datetime; inclusive lower bound on `started_at`. */
  from?: string;
  /** ISO datetime; exclusive upper bound on `started_at`. */
  to?: string;
  limit?: number;
  offset?: number;
}

function parseRecord(blob: string): ExperimentRecord {
  // The blob is whatever Pydantic emitted via `model_dump_json`. We trust
  // its shape (it's our own writer) but cast through unknown to make the
  // contract crossing explicit.
  return JSON.parse(blob) as ExperimentRecord;
}
