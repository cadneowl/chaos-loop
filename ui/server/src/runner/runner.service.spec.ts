import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { BadRequestException, NotFoundException } from '@nestjs/common';

import { RunnerService } from './runner.service';

describe('RunnerService', () => {
  let tmpDir: string;
  let plansDir: string;
  let svc: RunnerService;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'chaos-runner-'));
    plansDir = join(tmpDir, 'plans');
    mkdirSync(plansDir);
    process.env.CHAOS_PLANS_DIR = plansDir;
    // Force a known python so the constructor doesn't probe paths that
    // depend on test-runner CWD.
    process.env.CHAOS_PYTHON = 'python';
    svc = new RunnerService();
  });

  afterEach(() => {
    delete process.env.CHAOS_PLANS_DIR;
    delete process.env.CHAOS_PYTHON;
    rmSync(tmpDir, { recursive: true, force: true });
  });

  const seed = (name: string, body: string): void => {
    writeFileSync(join(plansDir, name), body, 'utf-8');
  };

  it('lists plan files with their header fields, sorted', async () => {
    seed(
      '02-cert.yaml',
      'experiment_id: exp-bbbbbbbbbbbb\ntitle: cert revocation\ntarget_app: otel-demo\n',
    );
    seed(
      '01-net.yaml',
      'experiment_id: exp-aaaaaaaaaaaa\ntitle: "network loss"\ntarget_app: otel-demo\n',
    );
    const plans = await svc.listPlans();
    expect(plans.map((p) => p.filename)).toEqual(['01-net.yaml', '02-cert.yaml']);
    expect(plans[0]).toEqual({
      filename: '01-net.yaml',
      experiment_id: 'exp-aaaaaaaaaaaa',
      title: 'network loss',
      target_app: 'otel-demo',
    });
  });

  it('lists nothing when the plans dir does not exist', async () => {
    rmSync(plansDir, { recursive: true, force: true });
    const plans = await svc.listPlans();
    expect(plans).toEqual([]);
  });

  it('rejects path traversal in run()', async () => {
    seed('01.yaml', 'experiment_id: exp-aaaaaaaaaaaa\ntitle: t\ntarget_app: a\n');
    await expect(svc.run({ filename: '../etc/passwd' })).rejects.toBeInstanceOf(BadRequestException);
    await expect(svc.run({ filename: '..\\evil.yaml' })).rejects.toBeInstanceOf(BadRequestException);
    await expect(svc.run({ filename: '/etc/hosts' })).rejects.toBeInstanceOf(BadRequestException);
    await expect(svc.run({ filename: 'a/b.yaml' })).rejects.toBeInstanceOf(BadRequestException);
  });

  it('rejects unknown filename with 404', async () => {
    await expect(svc.run({ filename: 'nope.yaml' })).rejects.toBeInstanceOf(NotFoundException);
  });

  it('rejects an invalid profile', async () => {
    seed('01.yaml', 'experiment_id: exp-aaaaaaaaaaaa\ntitle: t\ntarget_app: a\n');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await expect(svc.run({ filename: '01.yaml', profile: 'gpt-5' as any })).rejects.toBeInstanceOf(
      BadRequestException,
    );
  });

  it('skips files missing required header fields', async () => {
    seed('partial.yaml', 'experiment_id: exp-aaaaaaaaaaaa\n');
    const plans = await svc.listPlans();
    expect(plans).toHaveLength(0);
  });
});
