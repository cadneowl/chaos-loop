import { TestBed } from '@angular/core/testing';
import { provideAnimations } from '@angular/platform-browser/animations';

import type { FixProposal } from '../../../../core/contracts';
import { FixProposalTabComponent } from './fix-proposal-tab.component';

function proposal(overrides: Partial<FixProposal> = {}): FixProposal {
  return {
    experiment_id: 'exp-x',
    run_id: 'run-aaaaaaaaaaaa',
    action: 'code-patch',
    pr_url: null,
    confidence: 0.8,
    reasoning: '',
    files_touched: [],
    regression_test_added: false,
    is_draft: true,
    started_at: '2026-05-12T10:00:00Z',
    finished_at: null,
    ...overrides,
  };
}

describe('FixProposalTabComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [FixProposalTabComponent],
      providers: [provideAnimations()],
    }).compileComponents();
  });

  it('shows empty state when no proposal exists', async () => {
    const fixture = TestBed.createComponent(FixProposalTabComponent);
    fixture.componentRef.setInput('proposal', null);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fixture.nativeElement.textContent).toContain('No fix proposal');
  });

  it('renders the action badge and DRAFT marker', async () => {
    const fixture = TestBed.createComponent(FixProposalTabComponent);
    fixture.componentRef.setInput('proposal', proposal({ action: 'code-patch' }));
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('code-patch');
    expect(text).toContain('DRAFT');
  });

  it('renders the PR link with rel="noopener noreferrer" when pr_url is http(s)', async () => {
    const fixture = TestBed.createComponent(FixProposalTabComponent);
    fixture.componentRef.setInput(
      'proposal',
      proposal({ pr_url: 'https://example.invalid/owner/repo/pull/42' }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const link = (fixture.nativeElement as HTMLElement).querySelector('a[target="_blank"]');
    expect(link?.getAttribute('href')).toBe('https://example.invalid/owner/repo/pull/42');
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('blocks a javascript: pr_url and shows the unsafe fallback', async () => {
    const fixture = TestBed.createComponent(FixProposalTabComponent);
    fixture.componentRef.setInput(
      'proposal',
      proposal({ pr_url: 'javascript:alert(1)' }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('a[target="_blank"]')).toBeNull();
    const unsafe = root.querySelector('.pr-link.unsafe');
    expect(unsafe?.textContent).toContain('blocked');
    expect(unsafe?.textContent).toContain('javascript:alert(1)');
  });

  it('blocks a data: pr_url and shows the unsafe fallback', async () => {
    const fixture = TestBed.createComponent(FixProposalTabComponent);
    fixture.componentRef.setInput(
      'proposal',
      proposal({ pr_url: 'data:text/html,<script>alert(1)</script>' }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('a[target="_blank"]')).toBeNull();
    expect(root.querySelector('.pr-link.unsafe')?.textContent).toContain('blocked');
  });

  it('lists files_touched + the regression test chip when applicable', async () => {
    const fixture = TestBed.createComponent(FixProposalTabComponent);
    fixture.componentRef.setInput(
      'proposal',
      proposal({
        files_touched: ['services/cart/redis_client.py'],
        regression_test_added: true,
      }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('services/cart/redis_client.py');
    expect(text).toContain('regression test added');
  });

  it('renders multiline reasoning preserving line breaks', async () => {
    const fixture = TestBed.createComponent(FixProposalTabComponent);
    fixture.componentRef.setInput(
      'proposal',
      proposal({ reasoning: 'first line\nsecond line\nthird line' }),
    );
    fixture.detectChanges();
    await fixture.whenStable();
    const pre = (fixture.nativeElement as HTMLElement).querySelector('.reasoning');
    expect(pre?.textContent).toContain('first line');
    expect(pre?.textContent).toContain('second line');
  });
});
