import { Module } from '@nestjs/common';

import { ExperimentsModule } from './experiments/experiments.module';
import { HealthModule } from './health/health.module';
import { StoreModule } from './store/store.module';

@Module({
  imports: [StoreModule, HealthModule, ExperimentsModule],
})
export class AppModule {}
