import { TestBed } from '@angular/core/testing';
import { provideAnimations } from '@angular/platform-browser/animations';

import type { AgentInvocationLog } from '../../../../core/contracts';
import { LlmTelemetryTabComponent } from './llm-telemetry-tab.component';

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

describe('LlmTelemetryTabComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [LlmTelemetryTabComponent],
      providers: [provideAnimations()],
    }).compileComponents();
  });

  it('shows empty state when there are no invocations', async () => {
    const fixture = TestBed.createComponent(LlmTelemetryTabComponent);
    fixture.componentRef.setInput('invocations', []);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fixture.nativeElement.textContent).toContain('No agent invocations');
  });

  it('aggregates totals across invocations', async () => {
    const fixture = TestBed.createComponent(LlmTelemetryTabComponent);
    fixture.componentRef.setInput('invocations', [
      inv({
        agent: 'tester',
        spend_usd: 0.5,
        prompt_tokens: 100,
        completion_tokens: 50,
        tool_calls: [
          { name: 'read_file', arguments: '{}', result_preview: 'ok', is_error: false },
        ],
      }),
      inv({
        agent: 'fixer',
        spend_usd: 1.0,
        prompt_tokens: 200,
        completion_tokens: 100,
        tool_calls: [],
      }),
    ]);
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('300'); // prompt tokens
    expect(text).toContain('150'); // completion tokens
    expect(text).toContain('1.5000'); // total spend
  });

  it('groups by agent and sorts by spend descending', async () => {
    const fixture = TestBed.createComponent(LlmTelemetryTabComponent);
    fixture.componentRef.setInput('invocations', [
      inv({ agent: 'cheap', spend_usd: 0.01 }),
      inv({ agent: 'expensive', spend_usd: 5.0 }),
      inv({ agent: 'medium', spend_usd: 0.5 }),
    ]);
    fixture.detectChanges();
    await fixture.whenStable();
    const rows = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.by-agent tbody tr td:first-child'),
    ).map((td) => td.textContent?.trim());
    expect(rows).toEqual(['expensive', 'medium', 'cheap']);
  });

  it('reports 0 LLM-using invocations when none have spend or tokens', async () => {
    const fixture = TestBed.createComponent(LlmTelemetryTabComponent);
    fixture.componentRef.setInput('invocations', [
      inv({ spend_usd: null, prompt_tokens: null }),
      inv({ spend_usd: null, prompt_tokens: null }),
    ]);
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    // Static profile: 2 invocations, 0 with LLM
    expect(text).toContain('0 / 2');
  });

  it('renders tool call details inside the per-invocation panel', async () => {
    const fixture = TestBed.createComponent(LlmTelemetryTabComponent);
    fixture.componentRef.setInput('invocations', [
      inv({
        tool_calls: [
          { name: 'grep', arguments: '{"pattern":"TODO"}', result_preview: 'src/x.py:42', is_error: false },
          { name: 'read_file', arguments: '{"path":"x.py"}', result_preview: 'oops', is_error: true },
        ],
      }),
    ]);
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('grep');
    expect(text).toContain('read_file');
    expect(text).toContain('2 tool calls');
  });
});
