import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatExpansionModule } from '@angular/material/expansion';

import type { AgentInvocationLog } from '../../../../core/contracts';
import { sumSpend, sumTokens } from '../../../../core/token-utils';

interface AgentTotals {
  agent: string;
  invocations: number;
  spend_usd: number;
  prompt_tokens: number;
  completion_tokens: number;
  tool_calls: number;
}

/**
 * LLM telemetry — every invocation, every tool call, the cost.
 *
 * Top-level cards roll up totals by agent. Below: one row per invocation
 * with a drill-down that lists the tool calls + their preview output.
 *
 * Note that "spend_usd" is `null` for self-hosted Ollama runs (LiteLLM has
 * no pricing data for those). We render `null` as "—" rather than "$0.00"
 * so the operator can tell "free local LLM" from "no LLM call happened".
 */
@Component({
  selector: 'chaos-llm-telemetry-tab',
  standalone: true,
  imports: [DecimalPipe, MatCardModule, MatExpansionModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './llm-telemetry-tab.component.html',
  styleUrl: './llm-telemetry-tab.component.scss',
})
export class LlmTelemetryTabComponent {
  readonly invocations = input.required<AgentInvocationLog[]>();

  protected readonly totals = computed(() => {
    const invs = this.invocations();
    const tokens = sumTokens(invs);
    let calls = 0;
    let withLlm = 0;
    for (const inv of invs) {
      calls += inv.tool_calls.length;
      if ((inv.spend_usd ?? 0) > 0 || (inv.prompt_tokens ?? 0) > 0) {
        withLlm += 1;
      }
    }
    return {
      spend: sumSpend(invs),
      prompt: tokens.prompt,
      completion: tokens.completion,
      total_tokens: tokens.total,
      invocations: invs.length,
      tool_calls: calls,
      invocations_with_llm: withLlm,
    };
  });

  protected readonly byAgent = computed<AgentTotals[]>(() => {
    const map = new Map<string, AgentTotals>();
    for (const inv of this.invocations()) {
      const existing = map.get(inv.agent) ?? {
        agent: inv.agent,
        invocations: 0,
        spend_usd: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        tool_calls: 0,
      };
      existing.invocations += 1;
      existing.spend_usd += inv.spend_usd ?? 0;
      existing.prompt_tokens += inv.prompt_tokens ?? 0;
      existing.completion_tokens += inv.completion_tokens ?? 0;
      existing.tool_calls += inv.tool_calls.length;
      map.set(inv.agent, existing);
    }
    return [...map.values()].sort((a, b) => b.spend_usd - a.spend_usd);
  });

  protected readonly hasInvocations = computed(() => this.invocations().length > 0);

  protected formatTokens(n: number | null | undefined): string {
    return n === null || n === undefined ? '—' : String(n);
  }
}
