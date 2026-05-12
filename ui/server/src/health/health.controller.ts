import { Controller, Get } from '@nestjs/common';

import type { HealthDto } from './health.dto';

const startedAt = new Date();

@Controller('health')
export class HealthController {
  /**
   * Liveness probe. Returns 200 with a small status payload — enough for a
   * Kubernetes-shaped probe or a manual curl to confirm the API is up.
   */
  @Get()
  check(): HealthDto {
    return {
      status: 'ok',
      service: 'chaos-ui-server',
      startedAt: startedAt.toISOString(),
      now: new Date().toISOString(),
    };
  }
}
