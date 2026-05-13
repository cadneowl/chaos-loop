import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { rxResource } from '@angular/core/rxjs-interop';
import { FormsModule } from '@angular/forms';

import { ApiService } from '../../core/api.service';
import type { PlanFile, RunProfile } from '../../core/contracts';

@Component({
  selector: 'chaos-run',
  standalone: true,
  imports: [FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run.component.html',
  styleUrl: './run.component.scss',
})
export class RunComponent {
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);

  protected readonly plans = rxResource<PlanFile[], object>({
    params: () => ({}),
    stream: () => this.api.listPlans(),
  });

  protected readonly profile = signal<RunProfile>('static');
  protected readonly selected = signal<string | null>(null);
  protected readonly submitting = signal(false);
  protected readonly error = signal<string | null>(null);

  protected select(filename: string): void {
    this.selected.set(filename);
    this.error.set(null);
  }

  protected run(): void {
    const filename = this.selected();
    if (!filename) {
      this.error.set('Pick a plan to run.');
      return;
    }
    this.submitting.set(true);
    this.error.set(null);
    this.api.runExperiment(filename, this.profile()).subscribe({
      next: (response) => {
        // Fire-and-forget: redirect immediately to the detail page; the
        // orchestrator runs in the background and writes its record as
        // it goes.
        void this.router.navigate(['/experiments', response.experiment_id]);
      },
      error: (err: unknown) => {
        this.submitting.set(false);
        this.error.set(
          err && typeof err === 'object' && 'message' in err
            ? String((err as { message: unknown }).message)
            : 'Run failed.',
        );
      },
    });
  }
}
