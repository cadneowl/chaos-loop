import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';

import type { ExperimentRecord, ExperimentState } from '../../../../core/contracts';
import { sumTokens } from '../../../../core/token-utils';

/**
 * Overview — the first impression of an experiment.
 *
 * Shows the state, target, fault summary, key metrics, and abort reason
 * if any. Other tabs drill into specific slices; this one stays high-level.
 */
@Component({
  selector: 'chaos-overview-tab',
  standalone: true,
  imports: [DatePipe, DecimalPipe, MatCardModule, MatChipsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './overview-tab.component.html',
  styleUrl: './overview-tab.component.scss',
})
export class OverviewTabComponent {
  readonly record = input.required<ExperimentRecord>();

  protected readonly faultCount = computed(() => this.record().plan.faults.length);
  protected readonly primaryFault = computed(() => this.record().plan.faults[0] ?? null);
  protected readonly duration = computed(() => {
    const r = this.record();
    if (!r.finished_at) return null;
    return (new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000;
  });
  protected readonly totalTokens = computed(
    () => sumTokens(this.record().agent_invocations).total,
  );

  protected stateClass(state: ExperimentState): string {
    if (state === 'recorded') return 'state-ok';
    if (state === 'aborted') return 'state-aborted';
    if (state === 'paused') return 'state-paused';
    if (state.endsWith('_fail') || state.endsWith('_failed')) return 'state-failed';
    return 'state-running';
  }
}
