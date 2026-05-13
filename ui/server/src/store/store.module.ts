import { Module, Global } from '@nestjs/common';

import { SqliteReaderService } from './sqlite-reader.service';
import { SqliteWriterService } from './sqlite-writer.service';

/**
 * Global module — every other module that touches the experiment store
 * imports `SqliteReaderService` (and `SqliteWriterService` for the control
 * plane) from here. There's only one DB per process, so a singleton +
 * global feels appropriate.
 *
 * The writer is the **only** write surface the UI server has; it limits
 * itself to the three control-signal columns the orchestrator polls.
 */
@Global()
@Module({
  providers: [SqliteReaderService, SqliteWriterService],
  exports: [SqliteReaderService, SqliteWriterService],
})
export class StoreModule {}
