import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';

import type { DiagnosisReport } from '../../../../core/contracts';

@Component({
  selector: 'chaos-diagnosis-tab',
  standalone: true,
  imports: [DatePipe, DecimalPipe, MatCardModule, MatChipsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './diagnosis-tab.component.html',
  styleUrl: './diagnosis-tab.component.scss',
})
export class DiagnosisTabComponent {
  readonly diagnosis = input<DiagnosisReport | null>(null);

  protected readonly hypotheses = computed(() => this.diagnosis()?.hypotheses ?? []);
  protected readonly hasDiagnosis = computed(() => this.diagnosis() !== null);

  protected confidenceClass(c: number): string {
    if (c >= 0.7) return 'confidence-high';
    if (c >= 0.4) return 'confidence-medium';
    return 'confidence-low';
  }
}
