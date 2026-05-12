import { Module, Global } from '@nestjs/common';

import { SqliteReaderService } from './sqlite-reader.service';

/**
 * Global module — every other module that touches the experiment store
 * imports `SqliteReaderService` from here. There's only one DB per process,
 * so a singleton + global feels appropriate.
 */
@Global()
@Module({
  providers: [SqliteReaderService],
  exports: [SqliteReaderService],
})
export class StoreModule {}
