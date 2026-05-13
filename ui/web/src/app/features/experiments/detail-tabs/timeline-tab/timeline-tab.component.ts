import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { MatCardModule } from '@angular/material/card';

import type {
  AgentInvocationLog,
  ChaosTimeline,
} from '../../../../core/contracts';

/**
 * Single merged timeline: agent invocations + chaos events on the same time
 * axis. Each row is one happening; sorted chronologically.
 *
 * We keep this as a simple flat list for v1. ECharts swim-lane visualization
 * is a known follow-up — see ROADMAP.
 */
type UnifiedKind = 'invocation' | 'chaos';

interface UnifiedEntry {
  kind: UnifiedKind;
  timestamp: number; // epoch ms for sorting
  label: string;
  detail: string;
  duration_ms: number | null;
  ok: boolean;
}

@Component({
  selector: 'chaos-timeline-tab',
  standalone: true,
  imports: [DatePipe, DecimalPipe, MatCardModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './timeline-tab.component.html',
  styleUrl: './timeline-tab.component.scss',
})
export class TimelineTabComponent {
  readonly invocations = input.required<AgentInvocationLog[]>();
  readonly chaosTimeline = input<ChaosTimeline | null>(null);

  protected readonly entries = computed<UnifiedEntry[]>(() => {
    const out: UnifiedEntry[] = [];

    for (const inv of this.invocations()) {
      out.push({
        kind: 'invocation',
        timestamp: inv.started_at_ms,
        label: `${inv.agent}.${inv.method}`,
        detail: inv.input_summary || inv.output_summary || '',
        duration_ms: inv.duration_ms ?? null,
        ok: inv.ok,
      });
    }

    const ct = this.chaosTimeline();
    if (ct) {
      for (const ev of ct.events) {
        out.push({
          kind: 'chaos',
          timestamp: new Date(ev.timestamp).getTime(),
          label: `chaos.${ev.event}`,
          detail: `${ev.fault_name}${ev.detail ? ' · ' + ev.detail : ''}`,
          duration_ms: null,
          ok: ev.event !== 'error',
        });
      }
    }

    out.sort((a, b) => a.timestamp - b.timestamp);
    return out;
  });

  protected readonly hasEntries = computed(() => this.entries().length > 0);

  protected readonly relativeMs = computed(() => {
    const entries = this.entries();
    if (entries.length === 0) return 0;
    return entries[0].timestamp;
  });

  protected offsetSeconds(entry: UnifiedEntry): number {
    return (entry.timestamp - this.relativeMs()) / 1000;
  }
}
