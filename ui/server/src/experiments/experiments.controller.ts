import {
  BadRequestException,
  Body,
  Controller,
  DefaultValuePipe,
  Get,
  HttpCode,
  ParseIntPipe,
  Param,
  Post,
  Query,
} from '@nestjs/common';

import type {
  AbortReason,
  ControlSignals,
  ExperimentListResponse,
  ExperimentRecord,
  ExperimentState,
  ExperimentSummary,
} from '../contracts';
import { SqliteReaderService } from '../store/sqlite-reader.service';
import { SqliteWriterService } from '../store/sqlite-writer.service';

@Controller('experiments')
export class ExperimentsController {
  constructor(
    private readonly store: SqliteReaderService,
    private readonly writer: SqliteWriterService,
  ) {}

  /**
   * Paginated list of experiments. Default sort is started_at DESC.
   *
   * Query params:
   *   ?state=        ExperimentState filter (exact match)
   *   ?target_app=   target_app filter (exact match)
   *   ?from=         inclusive lower bound (ISO datetime)
   *   ?to=           exclusive upper bound (ISO datetime)
   *   ?limit=        page size (default 50, max 500)
   *   ?offset=       page offset (default 0)
   */
  @Get()
  list(
    @Query('state') state?: ExperimentState,
    @Query('target_app') target_app?: string,
    @Query('from') from?: string,
    @Query('to') to?: string,
    @Query('limit', new DefaultValuePipe(50), ParseIntPipe) limit?: number,
    @Query('offset', new DefaultValuePipe(0), ParseIntPipe) offset?: number,
  ): ExperimentListResponse {
    const records = this.store.listExperiments({
      state,
      target_app,
      from,
      to,
      limit,
      offset,
    });
    const total = this.store.countExperiments({ state, target_app, from, to });
    return {
      results: records.map(toSummary),
      total,
      limit: limit ?? 50,
      offset: offset ?? 0,
    };
  }

  /** Full ExperimentRecord. Throws 404 if no such experiment. */
  @Get(':id')
  detail(@Param('id') id: string): ExperimentRecord {
    return this.store.getExperiment(id);
  }

  /** Live operator signals on one experiment. Cheap (one row, one query). */
  @Get(':id/control')
  control(@Param('id') id: string): ControlSignals {
    return this.store.getControlSignals(id);
  }

  /**
   * Request a graceful pause at the next state-transition boundary. The
   * orchestrator polls the flag once per second and parks the experiment
   * in PAUSED until the matching resume call clears it. No-op on a
   * terminal experiment.
   */
  @Post(':id/pause')
  @HttpCode(204)
  pause(@Param('id') id: string): void {
    // Ensure the row exists; throws 404 with the same shape as detail().
    this.store.getExperiment(id);
    this.writer.setPause(id, true);
  }

  /** Clear the pause flag. The orchestrator resumes at the next poll. */
  @Post(':id/resume')
  @HttpCode(204)
  resume(@Param('id') id: string): void {
    this.store.getExperiment(id);
    this.writer.setPause(id, false);
  }

  /**
   * Request a graceful abort. The orchestrator picks up the flag, cleans
   * up any in-flight chaos CRDs, and transitions to ABORTED with the
   * supplied reason. Default reason is `user_kill`.
   */
  @Post(':id/abort')
  @HttpCode(204)
  abort(@Param('id') id: string, @Body() body: AbortRequestBody = {}): void {
    this.store.getExperiment(id);
    const reason: AbortReason = body.reason ?? 'user_kill';
    if (!ABORT_REASONS.has(reason)) {
      throw new BadRequestException(`invalid abort reason: ${reason}`);
    }
    this.writer.requestAbort(id, reason);
  }
}

interface AbortRequestBody {
  reason?: AbortReason;
}

const ABORT_REASONS: ReadonlySet<AbortReason> = new Set<AbortReason>([
  'baseline_unhealthy',
  'slo_breach',
  'budget_exceeded',
  'user_kill',
  'blast_radius_violation',
  'cluster_denied',
  'approval_rejected',
  'agent_failure',
]);

function toSummary(r: ExperimentRecord): ExperimentSummary {
  const started = new Date(r.started_at);
  const finished = r.finished_at ? new Date(r.finished_at) : null;
  const duration_seconds = finished ? (finished.getTime() - started.getTime()) / 1000 : null;
  return {
    experiment_id: r.experiment_id,
    title: r.plan.title,
    target_app: r.plan.target_app,
    state: r.state,
    started_at: r.started_at,
    finished_at: r.finished_at ?? null,
    duration_seconds,
    spend_usd: r.spend_usd,
    abort_reason: r.abort_reason ?? null,
    primary_fault: r.plan.faults[0]?.name ?? null,
    is_paused: r.state === 'paused',
  };
}
