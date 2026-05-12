import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { rxResource } from '@angular/core/rxjs-interop';

import { ApiService } from '../../../core/api.service';
import type { ExperimentState } from '../../../core/contracts';

const STATE_FILTERS: (ExperimentState | 'all')[] = [
  'all',
  'recorded',
  'aborted',
  'paused',
  'inject',
  'verify',
  'diagnose',
];

@Component({
  selector: 'chaos-experiments-list',
  standalone: true,
  imports: [DatePipe, DecimalPipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './experiments-list.component.html',
  styleUrl: './experiments-list.component.scss',
})
export class ExperimentsListComponent {
  private readonly api = inject(ApiService);

  protected readonly stateFilter = signal<ExperimentState | 'all'>('all');
  protected readonly stateOptions = STATE_FILTERS;

  /** Re-fires automatically when the filter signal changes.
   *  We do NOT `catchError` — the template's `experiments.error()` branch
   *  surfaces transport failures explicitly. */
  protected readonly experiments = rxResource({
    params: () => ({ state: this.stateFilter() }),
    stream: ({ params }) =>
      this.api.listExperiments(params.state === 'all' ? {} : { state: params.state }),
  });

  protected readonly hasResults = computed(
    () => (this.experiments.value()?.results.length ?? 0) > 0,
  );

  protected setFilter(value: ExperimentState | 'all'): void {
    this.stateFilter.set(value);
  }

  protected stateBadgeClass(state: ExperimentState): string {
    if (state === 'recorded') return 'badge badge-ok';
    if (state === 'aborted') return 'badge badge-aborted';
    if (state === 'paused') return 'badge badge-paused';
    if (state.endsWith('_fail') || state.endsWith('_failed')) return 'badge badge-failed';
    return 'badge badge-running';
  }
}
