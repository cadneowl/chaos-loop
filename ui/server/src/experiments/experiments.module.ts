import { Module } from '@nestjs/common';

import { ExperimentsController } from './experiments.controller';

@Module({
  controllers: [ExperimentsController],
})
export class ExperimentsModule {}
