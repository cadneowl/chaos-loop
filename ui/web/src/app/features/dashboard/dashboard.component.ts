import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { CurrencyPipe, DecimalPipe, PercentPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { rxResource } from '@angular/core/rxjs-interop';
import { NgxEchartsDirective } from 'ngx-echarts';
import type { EChartsCoreOption } from 'echarts/core';
import { combineLatest, map, type Observable } from 'rxjs';

import { ApiService } from '../../core/api.service';
import { baseOptions, PALETTE, tooltipPreset } from '../../core/charts/chart-theme';
import type {
  FindingsAggregates,
  FixesAggregates,
  LlmAggregates,
} from '../../core/contracts';

interface DashboardData {
  llm: LlmAggregates;
  findings: FindingsAggregates;
  fixes: FixesAggregates;
}

@Component({
  selector: 'chaos-dashboard',
  standalone: true,
  imports: [CurrencyPipe, DecimalPipe, PercentPipe, RouterLink, NgxEchartsDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './dashboard.component.html',
  styleUrl: './dashboard.component.scss',
})
export class DashboardComponent {
  private readonly api = inject(ApiService);

  /** One resource that fans out to the 3 endpoints in parallel. */
  protected readonly data = rxResource<DashboardData, object>({
    params: () => ({}),
    stream: (): Observable<DashboardData> =>
      combineLatest([
        this.api.getLlmAggregates(),
        this.api.getFindingsAggregates(),
        this.api.getFixesAggregates(),
      ]).pipe(map(([llm, findings, fixes]) => ({ llm, findings, fixes }))),
  });

  protected readonly spendByExperimentOptions = computed<EChartsCoreOption | null>(() => {
    const d = this.data.value();
    if (!d) return null;
    const rows = [...d.llm.by_experiment].reverse().slice(-20);
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', ...tooltipPreset },
      grid: { left: 56, right: 16, top: 16, bottom: 32, containLabel: true },
      xAxis: {
        type: 'category',
        data: rows.map((r) => r.experiment_id.slice(-6)),
        axisLabel: { fontSize: 10 },
      },
      yAxis: { type: 'value', name: 'spend (USD)' },
      series: [
        { type: 'bar', data: rows.map((r) => Number(r.spend_usd.toFixed(4))), itemStyle: { color: PALETTE[1] } },
      ],
    };
  });

  protected readonly fixClassOptions = computed<EChartsCoreOption | null>(() => {
    const d = this.data.value();
    if (!d || d.findings.by_fix_class.length === 0) return null;
    const rows = [...d.findings.by_fix_class].reverse();
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ...tooltipPreset },
      grid: { left: 140, right: 32, top: 16, bottom: 32 },
      xAxis: { type: 'value' },
      yAxis: { type: 'category', data: rows.map((r) => r.fix_class), axisLabel: { fontSize: 10 } },
      series: [{ type: 'bar', data: rows.map((r) => r.count), itemStyle: { color: PALETTE[2] } }],
    };
  });

  protected readonly throughputOptions = computed<EChartsCoreOption | null>(() => {
    const d = this.data.value();
    if (!d || d.fixes.by_day.length === 0) return null;
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', ...tooltipPreset },
      xAxis: { type: 'category', data: d.fixes.by_day.map((r) => r.date) },
      yAxis: { type: 'value', name: 'proposals' },
      series: [
        {
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 6,
          itemStyle: { color: PALETTE[3] },
          areaStyle: { color: 'rgba(220, 38, 38, 0.12)' },
          data: d.fixes.by_day.map((r) => r.count),
        },
      ],
    };
  });
}
