import { NestFactory } from '@nestjs/core';
import { Logger } from '@nestjs/common';
import { AppModule } from './app.module';

/**
 * Bootstrap. Binds to localhost only by default — this is a diagnostic UI for
 * an operator on the same machine as the orchestrator. Set CHAOS_UI_BIND to
 * "0.0.0.0" only when you've also set CHAOS_UI_API_KEY and accept the
 * security implications (see SECURITY.md).
 */
async function bootstrap() {
  const app = await NestFactory.create(AppModule, {
    logger: ['log', 'warn', 'error'],
  });
  app.setGlobalPrefix('api/v1');

  const port = Number(process.env.CHAOS_UI_PORT ?? 3000);
  const bind = process.env.CHAOS_UI_BIND ?? '127.0.0.1';
  await app.listen(port, bind);

  Logger.log(`chaos-ui-server listening on http://${bind}:${port}/api/v1`, 'Bootstrap');
}
bootstrap();
