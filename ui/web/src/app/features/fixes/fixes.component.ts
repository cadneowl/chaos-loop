import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';
import { rxResource } from '@angular/core/rxjs-interop';
import { NgxEchartsDirective } from 'ngx-echarts';
import type { EChartsCoreOption } from 'echarts/core';

import { ApiService } from '../../core/api.service';
import { baseOptions, PALETTE, tooltipPreset } from '../../core/charts/chart-theme';
import type { FixesAggregates } from '../../core/contracts';

@Component({
  selector: 'chaos-fixes',
  standalone: true,
  imports: [DecimalPipe, PercentPipe, NgxEchartsDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './fixes.component.html',
  styleUrl: './fixes.component.scss',
})
export class FixesComponent {
  private readonly api = inject(ApiService);

  protected readonly aggregates = rxResource<FixesAggregates, object>({
    params: () => ({}),
    stream: () => this.api.getFixesAggregates(),
  });

  /** Pie of recommended fix actions. */
  protected readonly byActionOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a || a.by_action.length === 0) return null;
    return {
      ...baseOptions,
      tooltip: { trigger: 'item', ...tooltipPreset },
      legend: { bottom: 0 },
      series: [
        {
          name: 'fix actions',
          type: 'pie',
          radius: ['40%', '70%'],
          label: { formatter: '{b}\n{c}' },
          data: a.by_action.map((r) => ({ name: r.action, value: r.count })),
        },
      ],
    };
  });

  /** Daily PR throughput — line chart of fix-proposal count per day. */
  protected readonly throughputOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a || a.by_day.length === 0) return null;
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', ...tooltipPreset },
      xAxis: { type: 'category', data: a.by_day.map((r) => r.date) },
      yAxis: { type: 'value', name: 'proposals' },
      series: [
        {
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 8,
          itemStyle: { color: PALETTE[1] },
          areaStyle: { color: 'rgba(245, 158, 11, 0.15)' },
          data: a.by_day.map((r) => r.count),
        },
      ],
    };
  });

  /** Top 20 most-touched files — horizontal bar so long paths read straight. */
  protected readonly topFilesOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a || a.by_file.length === 0) return null;
    const rows = [...a.by_file].reverse();
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ...tooltipPreset },
      grid: { left: 280, right: 32, top: 16, bottom: 32 },
      xAxis: { type: 'value', name: 'count' },
      yAxis: {
        type: 'category',
        data: rows.map((r) => r.path),
        axisLabel: { fontSize: 10, formatter: (v: string) => (v.length > 36 ? `…${v.slice(-35)}` : v) },
      },
      series: [
        {
          type: 'bar',
          data: rows.map((r) => r.count),
          itemStyle: { color: PALETTE[3] },
          label: { show: true, position: 'right' },
        },
      ],
    };
  });
}
