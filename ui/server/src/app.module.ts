import { Module } from '@nestjs/common';

import { AggregatesModule } from './aggregates/aggregates.module';
import { ExperimentsModule } from './experiments/experiments.module';
import { HealthModule } from './health/health.module';
import { StoreModule } from './store/store.module';

@Module({
  imports: [StoreModule, HealthModule, ExperimentsModule, AggregatesModule],
})
export class AppModule {}
