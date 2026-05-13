import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { DecimalPipe, CurrencyPipe, DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { rxResource } from '@angular/core/rxjs-interop';
import { NgxEchartsDirective } from 'ngx-echarts';
import type { EChartsCoreOption } from 'echarts/core';

import { ApiService } from '../../core/api.service';
import { baseOptions, PALETTE, tooltipPreset } from '../../core/charts/chart-theme';
import type { LlmAggregates } from '../../core/contracts';

@Component({
  selector: 'chaos-llm',
  standalone: true,
  imports: [DecimalPipe, CurrencyPipe, DatePipe, RouterLink, NgxEchartsDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './llm.component.html',
  styleUrl: './llm.component.scss',
})
export class LlmComponent {
  private readonly api = inject(ApiService);

  protected readonly aggregates = rxResource<LlmAggregates, object>({
    params: () => ({}),
    stream: () => this.api.getLlmAggregates(),
  });

  /** $ per experiment, newest first capped at 30 — readable bar count. */
  protected readonly spendOverTimeOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a) return null;
    const rows = [...a.by_experiment].reverse().slice(-30);
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', ...tooltipPreset },
      xAxis: {
        type: 'category',
        data: rows.map((r) => r.experiment_id),
        axisLabel: { rotate: -45, fontSize: 10, formatter: (v: string) => v.slice(-6) },
      },
      yAxis: { type: 'value', name: 'spend (USD)' },
      series: [
        {
          name: 'spend',
          type: 'bar',
          data: rows.map((r) => Number(r.spend_usd.toFixed(4))),
          itemStyle: { color: PALETTE[1] },
        },
      ],
    };
  });

  protected readonly perAgentOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a || a.by_agent.length === 0) return null;
    return {
      ...baseOptions,
      tooltip: { trigger: 'item', ...tooltipPreset },
      legend: { bottom: 0 },
      series: [
        {
          name: 'spend by agent',
          type: 'pie',
          radius: ['40%', '70%'],
          label: {
            formatter: (p: { name: string; value: number }) =>
              `${p.name}\n$${p.value.toFixed(4)}`,
          },
          data: a.by_agent.map((r) => ({ name: r.agent, value: Number(r.spend_usd.toFixed(4)) })),
        },
      ],
    };
  });

  /** tokens vs cost — one dot per experiment. Useful to spot expensive runs. */
  protected readonly tokensVsCostOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a) return null;
    return {
      ...baseOptions,
      tooltip: {
        trigger: 'item',
        ...tooltipPreset,
        formatter: (p: { data: [number, number, string] }) =>
          `${p.data[2]}<br/>${p.data[0].toLocaleString()} tokens · $${p.data[1].toFixed(4)}`,
      },
      xAxis: { type: 'value', name: 'tokens' },
      yAxis: { type: 'value', name: 'spend (USD)' },
      series: [
        {
          type: 'scatter',
          symbolSize: 12,
          itemStyle: { color: PALETTE[4], opacity: 0.7 },
          data: a.by_experiment.map((r) => [r.tokens, Number(r.spend_usd.toFixed(4)), r.title]),
        },
      ],
    };
  });
}
