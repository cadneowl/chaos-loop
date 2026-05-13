import { BadRequestException, Controller, Get, Query } from '@nestjs/common';

import { AggregatesService } from './aggregates.service';
import type {
  FindingsAggregates,
  FixesAggregates,
  LlmAggregates,
} from './aggregates.types';

/**
 * Cross-experiment projections. Each endpoint returns one focused shape
 * meant for one page of the SPA. All three accept a `?from=`/`?to=` time
 * window for scoping; omit them to fold over everything in the store.
 *
 * `from`/`to` are validated as ISO datetimes — otherwise a typo like
 * `?from=foo` would silently produce an empty result set instead of
 * surfacing the operator's mistake.
 */
@Controller('aggregates')
export class AggregatesController {
  constructor(private readonly aggregates: AggregatesService) {}

  @Get('llm')
  llm(@Query('from') from?: string, @Query('to') to?: string): LlmAggregates {
    return this.aggregates.getLlm(validateWindow(from, to));
  }

  @Get('findings')
  findings(
    @Query('from') from?: string,
    @Query('to') to?: string,
  ): FindingsAggregates {
    return this.aggregates.getFindings(validateWindow(from, to));
  }

  @Get('fixes')
  fixes(
    @Query('from') from?: string,
    @Query('to') to?: string,
  ): FixesAggregates {
    return this.aggregates.getFixes(validateWindow(from, to));
  }
}

function validateWindow(from?: string, to?: string): { from?: string; to?: string } {
  if (from && !isIsoDateTime(from)) {
    throw new BadRequestException(`'from' must be an ISO 8601 datetime, got: ${from}`);
  }
  if (to && !isIsoDateTime(to)) {
    throw new BadRequestException(`'to' must be an ISO 8601 datetime, got: ${to}`);
  }
  return { from, to };
}

function isIsoDateTime(s: string): boolean {
  return Number.isFinite(Date.parse(s));
}
