import { TestBed } from '@angular/core/testing';
import { provideAnimations } from '@angular/platform-browser/animations';

import type { DiagnosisReport } from '../../../../core/contracts';
import { DiagnosisTabComponent } from './diagnosis-tab.component';

function diagnosis(overrides: Partial<DiagnosisReport> = {}): DiagnosisReport {
  return {
    experiment_id: 'exp-x',
    run_id: 'run-aaaaaaaaaaaa',
    hypotheses: [],
    notes: '',
    started_at: '2026-05-12T10:00:00Z',
    finished_at: null,
    ...overrides,
  };
}

describe('DiagnosisTabComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DiagnosisTabComponent],
      providers: [provideAnimations()],
    }).compileComponents();
  });

  it('shows empty state when no diagnosis is present', async () => {
    const fixture = TestBed.createComponent(DiagnosisTabComponent);
    fixture.componentRef.setInput('diagnosis', null);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fixture.nativeElement.textContent).toContain('No diagnosis');
  });

  it('renders one card per hypothesis with its evidence', async () => {
    const fixture = TestBed.createComponent(DiagnosisTabComponent);
    fixture.componentRef.setInput(
      'diagnosis',
      diagnosis({
        hypotheses: [
          {
            summary: 'missing retry on cart service',
            confidence: 0.85,
            evidence: ['traces show 100% failure', 'no @retry in cart_client.py'],
            suggested_fix_class: 'missing-retry',
            affected_paths: ['services/cart/redis_client.py'],
          },
        ],
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('missing retry on cart service');
    expect(text).toContain('missing-retry');
    expect(text).toContain('traces show 100% failure');
    expect(text).toContain('services/cart/redis_client.py');
  });

  it('flags high-confidence hypotheses with the high class', async () => {
    const fixture = TestBed.createComponent(DiagnosisTabComponent);
    fixture.componentRef.setInput(
      'diagnosis',
      diagnosis({
        hypotheses: [
          {
            summary: 's',
            confidence: 0.9,
            evidence: [],
            suggested_fix_class: 'missing-retry',
            affected_paths: [],
          },
        ],
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.chip-confidence-high');
    expect(chip).not.toBeNull();
  });

  it('says "no evidence cited" when the hypothesis evidence list is empty', async () => {
    const fixture = TestBed.createComponent(DiagnosisTabComponent);
    fixture.componentRef.setInput(
      'diagnosis',
      diagnosis({
        hypotheses: [
          {
            summary: 's',
            confidence: 0.5,
            evidence: [],
            suggested_fix_class: 'working-as-intended',
            affected_paths: [],
          },
        ],
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fixture.nativeElement.textContent).toContain('No evidence cited');
  });

  it('shows a muted badge when a hypothesis is in suppressed_fingerprints', async () => {
    const fixture = TestBed.createComponent(DiagnosisTabComponent);
    fixture.componentRef.setInput(
      'diagnosis',
      diagnosis({
        hypotheses: [
          {
            id: 'aaaa11112222',
            summary: 'muted finding',
            confidence: 0.9,
            evidence: [],
            suggested_fix_class: 'missing-retry',
            affected_paths: [],
          },
          {
            id: 'bbbb33334444',
            summary: 'active finding',
            confidence: 0.8,
            evidence: [],
            suggested_fix_class: 'missing-timeout',
            affected_paths: [],
          },
        ],
        suppressed_fingerprints: ['aaaa11112222'],
        suppression_notes: { aaaa11112222: 'tracked in JIRA-1234' },
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();

    const root = fixture.nativeElement as HTMLElement;
    const cards = root.querySelectorAll('.hypothesis');
    expect(cards).toHaveLength(2);

    // First card: muted — gets the class + the badge + the reason as title.
    expect(cards[0].classList.contains('suppressed')).toBe(true);
    const badge = cards[0].querySelector('.muted-badge');
    expect(badge).not.toBeNull();
    expect(badge?.textContent?.trim()).toBe('muted');
    expect(badge?.getAttribute('title')).toBe('tracked in JIRA-1234');

    // Second card: active — neither class nor badge.
    expect(cards[1].classList.contains('suppressed')).toBe(false);
    expect(cards[1].querySelector('.muted-badge')).toBeNull();
  });
});
