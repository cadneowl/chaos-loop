import type { AgentInvocationLog } from './contracts';
import { safeHttpUrl, sumSpend, sumTokens } from './token-utils';

function inv(overrides: Partial<AgentInvocationLog>): AgentInvocationLog {
  return {
    agent: 'x',
    method: 'y',
    started_at_ms: 0,
    ok: true,
    input_summary: '',
    output_summary: '',
    tool_calls: [],
    ...overrides,
  };
}

describe('sumTokens', () => {
  it('treats null as zero', () => {
    expect(
      sumTokens([
        inv({ prompt_tokens: 100, completion_tokens: null }),
        inv({ prompt_tokens: null, completion_tokens: 50 }),
      ]),
    ).toEqual({ prompt: 100, completion: 50, total: 150 });
  });

  it('returns zeros for an empty list', () => {
    expect(sumTokens([])).toEqual({ prompt: 0, completion: 0, total: 0 });
  });

  it('preserves legitimate zero counts', () => {
    expect(sumTokens([inv({ prompt_tokens: 50, completion_tokens: 0 })])).toEqual({
      prompt: 50,
      completion: 0,
      total: 50,
    });
  });
});

describe('sumSpend', () => {
  it('treats null spend as zero (Ollama)', () => {
    expect(
      sumSpend([
        inv({ spend_usd: 0.5 }),
        inv({ spend_usd: null }),
        inv({ spend_usd: 0.25 }),
      ]),
    ).toBeCloseTo(0.75);
  });
});

describe('safeHttpUrl', () => {
  it('passes through https URLs unchanged', () => {
    expect(safeHttpUrl('https://github.com/owner/repo/pull/42')).toBe(
      'https://github.com/owner/repo/pull/42',
    );
  });

  it('passes through http URLs unchanged', () => {
    expect(safeHttpUrl('http://example.invalid/x')).toBe('http://example.invalid/x');
  });

  it('blocks javascript: URIs', () => {
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull();
  });

  it('blocks data: URIs (Chrome XSS vector)', () => {
    expect(safeHttpUrl('data:text/html,<script>alert(1)</script>')).toBeNull();
  });

  it('blocks file: URIs', () => {
    expect(safeHttpUrl('file:///etc/passwd')).toBeNull();
  });

  it('returns null for empty / null / undefined / unparseable', () => {
    expect(safeHttpUrl(null)).toBeNull();
    expect(safeHttpUrl(undefined)).toBeNull();
    expect(safeHttpUrl('')).toBeNull();
    expect(safeHttpUrl('not a url')).toBeNull();
  });
});
