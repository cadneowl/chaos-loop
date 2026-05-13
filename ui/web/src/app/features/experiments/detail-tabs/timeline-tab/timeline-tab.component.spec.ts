import { TestBed } from '@angular/core/testing';

import type {
  AgentInvocationLog,
  ChaosTimeline,
} from '../../../../core/contracts';
import { TimelineTabComponent } from './timeline-tab.component';

function inv(overrides: Partial<AgentInvocationLog>): AgentInvocationLog {
  return {
    agent: 'tester',
    method: 'baseline',
    started_at_ms: 0,
    ok: true,
    input_summary: '',
    output_summary: '',
    tool_calls: [],
    ...overrides,
  };
}

function chaosTimeline(overrides: Partial<ChaosTimeline> = {}): ChaosTimeline {
  return {
    experiment_id: 'exp-x',
    events: [],
    success: true,
    ...overrides,
  };
}

describe('TimelineTabComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TimelineTabComponent],
    }).compileComponents();
  });

  it('renders empty state when no entries', async () => {
    const fixture = TestBed.createComponent(TimelineTabComponent);
    fixture.componentRef.setInput('invocations', []);
    fixture.componentRef.setInput('chaosTimeline', null);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fixture.nativeElement.textContent).toContain('No timeline entries');
  });

  it('interleaves invocations + chaos events sorted by timestamp', async () => {
    const fixture = TestBed.createComponent(TimelineTabComponent);
    fixture.componentRef.setInput('invocations', [
      inv({ started_at_ms: 1_000_000_000_000, agent: 'tester', method: 'baseline' }),
      inv({ started_at_ms: 1_000_000_000_500, agent: 'chaos', method: 'execute' }),
    ]);
    fixture.componentRef.setInput(
      'chaosTimeline',
      chaosTimeline({
        events: [
          {
            timestamp: '2001-09-09T01:46:40.300Z', // 1_000_000_000_300
            fault_name: 'pod.kill',
            event: 'started',
            detail: '',
          },
        ],
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const labels = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.label'),
    ).map((el) => el.textContent?.trim());
    expect(labels).toEqual([
      'tester.baseline',
      'chaos.started',
      'chaos.execute',
    ]);
  });

  it('flags failed entries with the failed class', async () => {
    const fixture = TestBed.createComponent(TimelineTabComponent);
    fixture.componentRef.setInput('invocations', [
      inv({ ok: false, agent: 'fixer', method: 'propose_fix' }),
    ]);
    fixture.componentRef.setInput('chaosTimeline', null);
    fixture.detectChanges();
    await fixture.whenStable();
    const failed = (fixture.nativeElement as HTMLElement).querySelector('.failed');
    expect(failed).not.toBeNull();
  });
});
