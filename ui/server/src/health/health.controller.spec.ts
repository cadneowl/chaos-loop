import { Test, TestingModule } from '@nestjs/testing';

import { HealthController } from './health.controller';

describe('HealthController', () => {
  let controller: HealthController;

  beforeEach(async () => {
    const moduleRef: TestingModule = await Test.createTestingModule({
      controllers: [HealthController],
    }).compile();
    controller = moduleRef.get<HealthController>(HealthController);
  });

  it('returns ok status', () => {
    const result = controller.check();
    expect(result.status).toBe('ok');
    expect(result.service).toBe('chaos-ui-server');
  });

  it('includes a parseable startedAt timestamp', () => {
    const result = controller.check();
    expect(() => new Date(result.startedAt)).not.toThrow();
    expect(Number.isFinite(new Date(result.startedAt).getTime())).toBe(true);
  });

  it('returns a now timestamp at-or-after startedAt', () => {
    const result = controller.check();
    expect(new Date(result.now).getTime()).toBeGreaterThanOrEqual(
      new Date(result.startedAt).getTime(),
    );
  });
});
