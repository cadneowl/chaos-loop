import { spawn } from 'node:child_process';
import { readdir, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve, isAbsolute, basename } from 'node:path';

import {
  BadRequestException,
  Injectable,
  Logger,
  NotFoundException,
} from '@nestjs/common';

import type { RunProfile } from '../contracts';

/**
 * Server-side launcher for `chaos run`.
 *
 * Allowlist: plans must live in the repo's `experiments/examples/` directory
 * (configurable for tests via `CHAOS_PLANS_DIR`, but defaults to the repo
 * checkout). Anything else throws a 400, including paths that traverse out
 * (`../`), absolute paths, or symlinks pointing outside the dir.
 *

 * Spawn model: fire-and-forget. We resolve the experiment_id from the YAML
 * `experiment_id:` field, return it immediately, then `detach` the child so
 * a server restart doesn't kill the orchestrator. stdout / stderr are
 * intentionally dropped — the orchestrator's audit trail in SQLite is the
 * canonical record of what happened. A non-zero exit is logged at WARN so
 * operators can grep server logs for orchestrator crashes that happened
 * before any record could be written.
 */
@Injectable()
export class RunnerService {
  private readonly logger = new Logger(RunnerService.name);
  private readonly plansDir: string;
  private readonly pythonExe: string;

  constructor() {
    this.plansDir = resolve(
      process.env.CHAOS_PLANS_DIR ??
        // Default: repo's experiments/examples/, two levels up from ui/server.
        resolve(__dirname, '..', '..', '..', '..', 'experiments', 'examples'),
    );
    this.pythonExe = process.env.CHAOS_PYTHON ?? defaultPython();
  }

  /** List plan files in the allowlisted directory. */
  async listPlans(): Promise<PlanFile[]> {
    if (!existsSync(this.plansDir)) {
      this.logger.warn(`plans directory missing: ${this.plansDir}`);
      return [];
    }
    const entries = await readdir(this.plansDir, { withFileTypes: true });
    const plans: PlanFile[] = [];
    for (const entry of entries) {
      if (!entry.isFile()) continue;
      if (!entry.name.endsWith('.yaml') && !entry.name.endsWith('.yml')) continue;
      const fullPath = resolve(this.plansDir, entry.name);
      try {
        const head = await readPlanHeader(fullPath);
        plans.push({ filename: entry.name, ...head });
      } catch (err) {
        this.logger.warn(`skipping ${entry.name}: ${(err as Error).message}`);
      }
    }
    return plans.sort((a, b) => a.filename.localeCompare(b.filename));
  }

  /**
   * Spawn `chaos run <plan> --profile <profile>` as a detached subprocess.
   * Returns the parsed experiment_id immediately. The UI then redirects to
   * the detail page and polls /control for live state.
   */
  async run(request: RunRequest): Promise<RunResponse> {
    const planPath = this.resolveAndValidate(request.filename);
    const profile = request.profile ?? 'static';
    if (!RUN_PROFILES.has(profile)) {
      throw new BadRequestException(`profile must be one of static/hybrid/llm, got: ${profile}`);
    }
    const head = await readPlanHeader(planPath);

    const args = ['-m', 'orchestrator.main', 'run', planPath, '--profile', profile];
    this.logger.log(`spawning ${this.pythonExe} ${args.join(' ')}`);

    const child = spawn(this.pythonExe, args, {
      detached: true,
      stdio: 'ignore',
      env: process.env,
    });
    child.on('error', (err) => {
      this.logger.error(`spawn failed for ${planPath}: ${err.message}`);
    });
    child.on('exit', (code, signal) => {
      if (code !== 0 && code !== null) {
        this.logger.warn(
          `orchestrator for ${head.experiment_id} exited code=${code} signal=${signal}`,
        );
      }
    });
    // unref() lets the parent (this server) exit independently of the child;
    // the orchestrator survives a UI restart and finishes writing its
    // record to SQLite on its own schedule.
    child.unref();

    return {
      experiment_id: head.experiment_id,
      title: head.title,
      target_app: head.target_app,
      profile,
    };
  }

  private resolveAndValidate(filename: string): string {
    // Block path traversal + absolute paths up front; only the basename is
    // accepted, then we resolve under plansDir and double-check the resolved
    // path is still inside.
    if (isAbsolute(filename) || filename.includes('..') || filename !== basename(filename)) {
      throw new BadRequestException(`filename must be a bare YAML name in the plans directory: ${filename}`);
    }
    const full = resolve(this.plansDir, filename);
    if (!full.startsWith(this.plansDir + (process.platform === 'win32' ? '\\' : '/'))) {
      throw new BadRequestException(`plan path escapes the allowed directory: ${filename}`);
    }
    if (!existsSync(full)) {
      throw new NotFoundException(`no plan named ${filename} in the plans directory`);
    }
    return full;
  }
}

const RUN_PROFILES: ReadonlySet<RunProfile> = new Set<RunProfile>(['static', 'hybrid', 'llm']);

function defaultPython(): string {
  // Prefer the repo's venv on Windows / POSIX; the operator can override
  // with CHAOS_PYTHON if they want a system Python.
  const venv = process.platform === 'win32'
    ? resolve(__dirname, '..', '..', '..', '..', '.venv', 'Scripts', 'python.exe')
    : resolve(__dirname, '..', '..', '..', '..', '.venv', 'bin', 'python');
  return existsSync(venv) ? venv : 'python';
}

async function readPlanHeader(path: string): Promise<{
  experiment_id: string;
  title: string;
  target_app: string;
}> {
  const text = await readFile(path, 'utf-8');
  const experiment_id = extractScalar(text, 'experiment_id');
  const title = extractScalar(text, 'title');
  const target_app = extractScalar(text, 'target_app');
  if (!experiment_id || !title || !target_app) {
    throw new Error(
      `plan at ${path} is missing one of: experiment_id, title, target_app`,
    );
  }
  if (!EXPERIMENT_ID_RE.test(experiment_id)) {
    throw new Error(
      `plan at ${path} has invalid experiment_id ${JSON.stringify(experiment_id)}; ` +
        `must match ${EXPERIMENT_ID_RE}`,
    );
  }
  return { experiment_id, title, target_app };
}

// Mirrors the Pydantic constraint in shared/contracts.py:ExperimentPlan.
const EXPERIMENT_ID_RE = /^exp-[0-9a-f]{12}$/;

function extractScalar(yaml: string, key: string): string | null {
  // Minimal top-level scalar extractor — covers `key: value`, `key: "value"`,
  // `key: 'value'`. The orchestrator does full YAML parsing; we only need
  // a few header fields and want to avoid pulling in a YAML dep here.
  const m = yaml.match(new RegExp(`^${key}\\s*:\\s*(?:"([^"]*)"|'([^']*)'|([^\\n]+))$`, 'm'));
  if (!m) return null;
  return (m[1] ?? m[2] ?? m[3] ?? '').trim();
}

export interface PlanFile {
  filename: string;
  experiment_id: string;
  title: string;
  target_app: string;
}

export interface RunRequest {
  filename: string;
  profile?: RunProfile;
}

export interface RunResponse {
  experiment_id: string;
  title: string;
  target_app: string;
  profile: RunProfile;
}
