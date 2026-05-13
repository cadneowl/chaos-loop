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
});
