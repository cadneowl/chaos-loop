import { Module } from '@nestjs/common';

import { AggregatesController } from './aggregates.controller';
import { AggregatesService } from './aggregates.service';

@Module({
  controllers: [AggregatesController],
  providers: [AggregatesService],
})
export class AggregatesModule {}
