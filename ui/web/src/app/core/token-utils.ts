import type { AgentInvocationLog } from './contracts';

/**
 * Sum prompt + completion tokens across a list of invocations, treating
 * `null` as "no data" (== 0). Used by both Overview and LLM-telemetry
 * tabs to keep the calculation in one place.
 */
export function sumTokens(invocations: AgentInvocationLog[]): {
  prompt: number;
  completion: number;
  total: number;
} {
  let prompt = 0;
  let completion = 0;
  for (const inv of invocations) {
    prompt += inv.prompt_tokens ?? 0;
    completion += inv.completion_tokens ?? 0;
  }
  return { prompt, completion, total: prompt + completion };
}

/**
 * Sum LLM spend across invocations. Null spend is treated as 0
 * (e.g., self-hosted Ollama where LiteLLM has no pricing data).
 */
export function sumSpend(invocations: AgentInvocationLog[]): number {
  let total = 0;
  for (const inv of invocations) {
    total += inv.spend_usd ?? 0;
  }
  return total;
}

/**
 * Validate a URL for safe use as an `[href]`. Returns the URL if it parses
 * AND uses http/https; null otherwise.
 *
 * Angular's template sanitizer does not block `data:` or `javascript:`
 * URIs in `[href]` reliably across browsers — `data:text/html` is fully
 * functional XSS in Chrome. We restrict to http(s) explicitly so a
 * malicious `pr_url` (e.g., supply-chain compromise of the fixer agent)
 * can't execute as the UI origin.
 */
export function safeHttpUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return url;
  } catch {
    // not a valid URL
  }
  return null;
}
