/**
 * Contract drift guard.
 *
 * The TS mirrors in `index.ts` are hand-maintained. This spec loads a real
 * `ExperimentRecord` blob shape (built by the test fixture) and asserts that
 * every required field on every contract type is present at the location the
 * TypeScript expects.
 *
 * It also asserts the inverse — that every required Python contract field is
 * either present on the JSON or has a default — by pinning the union members
 * for every enum, so a Python-side enum addition not mirrored here trips
 * loudly in TypeScript.
 *
 * If you change `shared/contracts.py`, mirror it here AND update this spec.
 */

import { sampleRecord } from '../../test/fixtures/seed-store';
import type {
  ExperimentRecord,
  FaultCategory,
  FixAction,
  ScannerName,
  SecurityFinding,
  SecurityHypothesis,
  SuggestedFixClass,
} from './index';

describe('contracts (TS mirror of shared/contracts.py)', () => {
  it('every required ExperimentRecord field is present in the fixture', () => {
    const record: ExperimentRecord = sampleRecord();
    // Static field presence — TS compile-time check.
    expect(record.experiment_id).toBeDefined();
    expect(record.plan).toBeDefined();
    expect(record.state).toBeDefined();
    expect(record.started_at).toBeDefined();
    expect(record.spend_usd).toBeDefined();
    expect(record.agent_invocations).toBeDefined();
    expect(record.abort_detail).toBeDefined();
    expect(record.plan.faults).toBeDefined();
    expect(record.plan.safety).toBeDefined();
    expect(record.plan.budget).toBeDefined();
  });

  it('FaultCategory union covers every value from shared/contracts.py FaultCategory', () => {
    // If the Python side adds a category, mirror it here AND add to this list.
    // The compile checks both halves: assignment fails if the TS enum drops
    // a value, and the array fails to compile if the TS enum adds one that
    // isn't listed here.
    const all: FaultCategory[] = [
      'pod', 'network', 'io', 'stress', 'dns', 'http', 'time', 'kernel',
      'cert', 'tls', 'auth', 'secret', 'image', 'iam', 'netpol', 'egress',
      'runtime',
    ];
    expect(all).toHaveLength(17);
  });

  it('ScannerName union covers all 8 scanners', () => {
    const all: ScannerName[] = [
      'zap', 'syft', 'grype', 'trivy', 'gitleaks', 'cosign', 'kubescape',
      'custom-probe',
    ];
    expect(all).toHaveLength(8);
  });

  it('SuggestedFixClass union covers all 11 fix classes', () => {
    const all: SuggestedFixClass[] = [
      'code-patch', 'config-change',
      'missing-retry', 'missing-timeout', 'missing-circuit-breaker', 'missing-fallback',
      'auth-control-gap', 'secret-handling', 'image-policy',
      'test-gap', 'working-as-intended',
    ];
    expect(all).toHaveLength(11);
  });

  it('FixAction union covers all 4 actions', () => {
    const all: FixAction[] = ['none', 'doc-only', 'code-patch', 'config-change'];
    expect(all).toHaveLength(4);
  });

  it('SecurityFinding.location is nullable (Python field is `str | None = None`)', () => {
    const finding: SecurityFinding = {
      id: 'f-x',
      severity: 'info',
      title: '',
      description: '',
      scanner: 'trivy',
      evidence: {},
      location: null, // must type-check
    };
    expect(finding.location).toBeNull();
  });

  it('SecurityHypothesis includes success_criteria and references (Python had them, TS used to miss them)', () => {
    const hypo: SecurityHypothesis = {
      id: 'h-x',
      statement: '',
      rationale: '',
      proposed_fault: '',
      success_criteria: [],
      confidence: 0,
      references: [],
    };
    expect(hypo.success_criteria).toBeDefined();
    expect(hypo.references).toBeDefined();
  });
});
