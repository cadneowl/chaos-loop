import { TestBed } from '@angular/core/testing';
import { provideAnimations } from '@angular/platform-browser/animations';

import type { ExperimentRecord } from '../../../../core/contracts';
import { OverviewTabComponent } from './overview-tab.component';

function makeRecord(overrides: Partial<ExperimentRecord> = {}): ExperimentRecord {
  return {
    experiment_id: 'exp-aaaaaaaaaaaa',
    state: 'recorded',
    started_at: '2026-05-12T10:00:00Z',
    finished_at: '2026-05-12T10:05:00Z',
    abort_reason: null,
    abort_detail: '',
    spend_usd: 1.23,
    plan: {
      experiment_id: 'exp-aaaaaaaaaaaa',
      title: 'sample',
      target_app: 'otel-demo',
      target_repo: null,
      faults: [
        {
          category: 'network',
          name: 'network.loss',
          target_selector: { app: 'x' },
          parameters: {},
          duration_seconds: 60,
          requires_approval: false,
          rationale: 'r',
        },
      ],
      safety: {
        cluster_context: 'kind-test',
        namespace: 'default',
        max_pods_affected: 1,
        max_duration_seconds: 120,
        allow_multi_fault: false,
        require_namespace_annotation: false,
        forbidden_cluster_substrings: [],
      },
      budget: { soft_cap_usd: 1, hard_cap_usd: 5, wall_clock_seconds: 900 },
      quiet_window_pre_seconds: 60,
      quiet_window_post_seconds: 60,
      created_at: '2026-05-12T10:00:00Z',
    },
    tester_baseline: null,
    security_baseline: null,
    chaos_timeline: null,
    tester_verify: null,
    security_verify: null,
    diagnosis: null,
    fix_proposal: null,
    agent_invocations: [
      {
        agent: 'tester',
        method: 'baseline',
        started_at_ms: 0,
        ok: true,
        input_summary: '',
        output_summary: '',
        prompt_tokens: 100,
        completion_tokens: 50,
        spend_usd: 0.01,
        tool_calls: [],
      },
    ],
    ...overrides,
  };
}

describe('OverviewTabComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [OverviewTabComponent],
      providers: [provideAnimations()],
    }).compileComponents();
  });

  it('renders the experiment title and target_app', async () => {
    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('record', makeRecord());
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('sample');
    expect(text).toContain('otel-demo');
  });

  it('shows the abort reason when present', async () => {
    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput(
      'record',
      makeRecord({ state: 'aborted', abort_reason: 'user_kill' }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('user_kill');
  });

  it('aggregates token totals across all invocations', async () => {
    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput(
      'record',
      makeRecord({
        agent_invocations: [
          {
            agent: 'tester',
            method: 'baseline',
            started_at_ms: 0,
            ok: true,
            input_summary: '',
            output_summary: '',
            prompt_tokens: 100,
            completion_tokens: 50,
            tool_calls: [],
          },
          {
            agent: 'fixer',
            method: 'propose_fix',
            started_at_ms: 1,
            ok: true,
            input_summary: '',
            output_summary: '',
            prompt_tokens: 200,
            completion_tokens: 75,
            tool_calls: [],
          },
        ],
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    // 100+50 + 200+75 = 425
    expect(text).toContain('425');
  });

  it('handles a record with no finished_at without throwing', async () => {
    const fixture = TestBed.createComponent(OverviewTabComponent);
    fixture.componentRef.setInput('record', makeRecord({ finished_at: null }));
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fixture.nativeElement.textContent).toContain('—');
  });
});
