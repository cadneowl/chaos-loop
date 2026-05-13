import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { DecimalPipe, DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { rxResource } from '@angular/core/rxjs-interop';
import { NgxEchartsDirective } from 'ngx-echarts';
import type { EChartsCoreOption } from 'echarts/core';

import { ApiService } from '../../core/api.service';
import { baseOptions, PALETTE, tooltipPreset } from '../../core/charts/chart-theme';
import type { FindingsAggregates } from '../../core/contracts';

@Component({
  selector: 'chaos-findings',
  standalone: true,
  imports: [DecimalPipe, DatePipe, RouterLink, NgxEchartsDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './findings.component.html',
  styleUrl: './findings.component.scss',
})
export class FindingsComponent {
  private readonly api = inject(ApiService);

  protected readonly aggregates = rxResource<FindingsAggregates, object>({
    params: () => ({}),
    stream: () => this.api.getFindingsAggregates(),
  });

  /** Horizontal bar of fix-class incidence — orientation makes long labels readable. */
  protected readonly byFixClassOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a || a.by_fix_class.length === 0) return null;
    const rows = [...a.by_fix_class].reverse();
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ...tooltipPreset },
      grid: { left: 160, right: 32, top: 16, bottom: 32 },
      xAxis: { type: 'value', name: 'count' },
      yAxis: { type: 'category', data: rows.map((r) => r.fix_class) },
      series: [
        {
          type: 'bar',
          data: rows.map((r) => r.count),
          itemStyle: { color: PALETTE[2] },
          label: { show: true, position: 'right' },
        },
      ],
    };
  });

  protected readonly confidenceHistogramOptions = computed<EChartsCoreOption | null>(() => {
    const a = this.aggregates.value();
    if (!a) return null;
    return {
      ...baseOptions,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, ...tooltipPreset },
      xAxis: { type: 'category', data: a.confidence_histogram.map((b) => b.bucket), name: 'confidence' },
      yAxis: { type: 'value', name: 'hypotheses' },
      series: [
        {
          type: 'bar',
          data: a.confidence_histogram.map((b) => b.count),
          itemStyle: { color: PALETTE[5] },
        },
      ],
    };
  });

  protected confidenceClass(c: number): string {
    if (c >= 0.7) return 'high';
    if (c >= 0.4) return 'medium';
    return 'low';
  }
}
