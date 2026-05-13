import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { JsonPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatTabsModule } from '@angular/material/tabs';
import { rxResource } from '@angular/core/rxjs-interop';

import { ApiService } from '../../../core/api.service';
import type { ExperimentRecord } from '../../../core/contracts';
import { DiagnosisTabComponent } from '../detail-tabs/diagnosis-tab/diagnosis-tab.component';
import { FixProposalTabComponent } from '../detail-tabs/fix-proposal-tab/fix-proposal-tab.component';
import { LlmTelemetryTabComponent } from '../detail-tabs/llm-telemetry-tab/llm-telemetry-tab.component';
import { OverviewTabComponent } from '../detail-tabs/overview-tab/overview-tab.component';
import { TimelineTabComponent } from '../detail-tabs/timeline-tab/timeline-tab.component';

@Component({
  selector: 'chaos-experiment-detail',
  standalone: true,
  imports: [
    JsonPipe,
    RouterLink,
    MatTabsModule,
    OverviewTabComponent,
    TimelineTabComponent,
    LlmTelemetryTabComponent,
    DiagnosisTabComponent,
    FixProposalTabComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './experiment-detail.component.html',
  styleUrl: './experiment-detail.component.scss',
})
export class ExperimentDetailComponent {
  private readonly api = inject(ApiService);

  /** Signal input bound from the route param via `withComponentInputBinding()`. */
  readonly id = input.required<string>();

  /** Re-fires whenever `id()` changes. Errors surface via `record.error()`. */
  protected readonly record = rxResource<ExperimentRecord, { id: string }>({
    params: () => ({ id: this.id() }),
    stream: ({ params }) => this.api.getExperiment(params.id),
  });

  protected readonly invocations = computed(() => this.record.value()?.agent_invocations ?? []);
}
