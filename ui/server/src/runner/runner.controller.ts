import { Body, Controller, Get, HttpCode, Post } from '@nestjs/common';

import { RunnerService } from './runner.service';
import type { PlanFile, RunRequest, RunResponse } from './runner.service';

/**
 * Control plane: list known plans and start new experiments.
 *
 * `GET /api/v1/plans`            — what can the UI offer to run?
 * `POST /api/v1/experiments/run` — start one by filename + profile.
 *
 * The runner only accepts plan filenames in the allowlisted directory
 * (default: `experiments/examples/`). Path traversal and absolute paths
 * are rejected with 400.
 */
@Controller()
export class RunnerController {
  constructor(private readonly runner: RunnerService) {}

  @Get('plans')
  list(): Promise<PlanFile[]> {
    return this.runner.listPlans();
  }

  @Post('experiments/run')
  @HttpCode(202)
  run(@Body() body: RunRequest): Promise<RunResponse> {
    return this.runner.run(body);
  }
}
