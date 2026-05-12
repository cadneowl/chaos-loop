import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { DatePipe, DecimalPipe, JsonPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { rxResource } from '@angular/core/rxjs-interop';

import { ApiService } from '../../../core/api.service';
import type { ExperimentRecord } from '../../../core/contracts';

@Component({
  selector: 'chaos-experiment-detail',
  standalone: true,
  imports: [DatePipe, DecimalPipe, JsonPipe, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './experiment-detail.component.html',
  styleUrl: './experiment-detail.component.scss',
})
export class ExperimentDetailComponent {
  private readonly api = inject(ApiService);

  /** Signal input bound from the route param via `withComponentInputBinding()`.
   *  This is the Angular 21 idiom — re-emits cleanly when the param changes
   *  via route-reuse, no manual ngOnInit shim needed. */
  readonly id = input.required<string>();

  /** Resource re-fires whenever `id()` changes. The user-facing error path
   *  is the `record.error()` accessor in the template — we do NOT
   *  `catchError` here because that turns transport failures into silent
   *  empty states. */
  protected readonly record = rxResource<ExperimentRecord, { id: string }>({
    params: () => ({ id: this.id() }),
    stream: ({ params }) => this.api.getExperiment(params.id),
  });

  protected readonly invocations = computed(() => this.record.value()?.agent_invocations ?? []);

  protected readonly totalSpend = computed(() => this.record.value()?.spend_usd ?? 0);

  protected readonly totalTokens = computed(() => {
    const inv = this.invocations();
    let p = 0;
    let c = 0;
    for (const i of inv) {
      p += i.prompt_tokens ?? 0;
      c += i.completion_tokens ?? 0;
    }
    return { prompt: p, completion: c, total: p + c };
  });
}
