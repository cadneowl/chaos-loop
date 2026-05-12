import { Test, TestingModule } from '@nestjs/testing';
import { INestApplication } from '@nestjs/common';
import request from 'supertest';
import { App } from 'supertest/types';
import { AppModule } from './../src/app.module';

describe('chaos-ui-server (e2e)', () => {
  let app: INestApplication<App>;

  beforeEach(async () => {
    const moduleFixture: TestingModule = await Test.createTestingModule({
      imports: [AppModule],
    }).compile();

    app = moduleFixture.createNestApplication();
    app.setGlobalPrefix('api/v1');
    await app.init();
  });

  afterEach(async () => {
    await app.close();
  });

  it('GET /api/v1/health returns ok status', async () => {
    const response = await request(app.getHttpServer())
      .get('/api/v1/health')
      .expect(200);
    expect(response.body).toMatchObject({
      status: 'ok',
      service: 'chaos-ui-server',
    });
    expect(response.body.startedAt).toBeDefined();
    expect(response.body.now).toBeDefined();
  });

  it('unknown routes return 404', () => {
    return request(app.getHttpServer()).get('/api/v1/nope').expect(404);
  });
});
