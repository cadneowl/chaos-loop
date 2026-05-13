import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';

import type { FixProposal } from '../../../../core/contracts';
import { safeHttpUrl } from '../../../../core/token-utils';

@Component({
  selector: 'chaos-fix-proposal-tab',
  standalone: true,
  imports: [DatePipe, DecimalPipe, MatCardModule, MatChipsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './fix-proposal-tab.component.html',
  styleUrl: './fix-proposal-tab.component.scss',
})
export class FixProposalTabComponent {
  readonly proposal = input<FixProposal | null>(null);

  protected readonly hasProposal = computed(() => this.proposal() !== null);

  /** http(s)-only PR URL or null. Defends against `data:` / `javascript:`
   *  URIs that would otherwise execute as the UI origin. */
  protected readonly prHref = computed(() => safeHttpUrl(this.proposal()?.pr_url ?? null));

  protected actionClass(action: string): string {
    if (action === 'none') return 'action-none';
    if (action === 'doc-only') return 'action-doc';
    return 'action-act';
  }
}
