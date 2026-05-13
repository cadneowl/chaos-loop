import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { JsonPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { MatTabsModule } from '@angular/material/tabs';
import { MatButtonModule } from '@angular/material/button';
import { rxResource } from '@angular/core/rxjs-interop';

import { ApiService } from '../../../core/api.service';
import type { ExperimentRecord, ExperimentState } from '../../../core/contracts';
import { DiagnosisTabComponent } from '../detail-tabs/diagnosis-tab/diagnosis-tab.component';
import { FixProposalTabComponent } from '../detail-tabs/fix-proposal-tab/fix-proposal-tab.component';
import { LlmTelemetryTabComponent } from '../detail-tabs/llm-telemetry-tab/llm-telemetry-tab.component';
import { OverviewTabComponent } from '../detail-tabs/overview-tab/overview-tab.component';
import { TimelineTabComponent } from '../detail-tabs/timeline-tab/timeline-tab.component';

/**
 * Action-bar visibility: a control signal (pause / resume / abort) is only
 * meaningful while the orchestrator is still polling — i.e. for any
 * experiment that has NOT yet reached a terminal state.
 *
 * This set is the **complement** of `_LIVE_STATES` in
 * `orchestrator/main.py`. Keep them in sync: a state added to the
 * orchestrator's live set must NOT appear here. `baseline_fail` and
 * `inject_failed` are LIVE in the orchestrator's view (the operator can
 * still abort an experiment stuck in a fail state before the next
 * transition), so they are deliberately NOT terminal here.
 */
const TERMINAL_STATES: ReadonlySet<ExperimentState> = new Set<ExperimentState>([
  'steady',
  'fix_proposed',
  'fix_declined',
  'aborted',
  'recorded',
]);

@Component({
  selector: 'chaos-experiment-detail',
  standalone: true,
  imports: [
    JsonPipe,
    RouterLink,
    MatTabsModule,
    MatButtonModule,
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

  /** True for any non-terminal state — the action bar is meaningful only then. */
  protected readonly isLive = computed(() => {
    const state = this.record.value()?.state;
    if (!state) return false;
    return !TERMINAL_STATES.has(state);
  });

  protected readonly isPaused = computed(() => this.record.value()?.state === 'paused');

  /** Inline status banner shown after a control action fires. */
  protected readonly actionMessage = signal<string | null>(null);
  protected readonly actionError = signal<string | null>(null);
  protected readonly actionPending = signal(false);

  protected pause(): void {
    this.fire('Pausing experiment…', this.api.pauseExperiment(this.id()));
  }

  protected resume(): void {
    this.fire('Resuming experiment…', this.api.resumeExperiment(this.id()));
  }

  protected abort(): void {
    if (!confirm('Abort this experiment? The orchestrator will tear down any in-flight chaos.')) {
      return;
    }
    this.fire('Abort requested…', this.api.abortExperiment(this.id()));
  }

  private fire(pendingMessage: string, action: ReturnType<ApiService['pauseExperiment']>): void {
    this.actionPending.set(true);
    this.actionError.set(null);
    this.actionMessage.set(pendingMessage);
    action.subscribe({
      next: () => {
        this.actionMessage.set('Done. The orchestrator will pick this up at the next state boundary.');
        // Reload the record to pick up the new state / pause flag, and hold
        // `actionPending` until the resource finishes so the buttons can't
        // be re-clicked against a stale record.
        this.record.reload();
        queueMicrotask(() => this.actionPending.set(false));
      },
      error: (err: unknown) => {
        this.actionPending.set(false);
        this.actionMessage.set(null);
        this.actionError.set(
          err && typeof err === 'object' && 'message' in err
            ? String((err as { message: unknown }).message)
            : 'Action failed.',
        );
      },
    });
  }
}
