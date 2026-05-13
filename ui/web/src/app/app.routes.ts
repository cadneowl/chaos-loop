import { Routes } from '@angular/router';

/**
 * Lazy-loaded route tree. Each feature is its own bundle.
 *
 * Top-level pages:
 *   /              dashboard — combined snapshot of LLM / findings / fixes
 *   /experiments   per-experiment list + detail (PR B.1 / B.2)
 *   /llm           cross-experiment LLM telemetry rollups
 *   /findings      cross-experiment diagnosis hypothesis rollups
 *   /fixes         cross-experiment fix-proposal outcomes
 */
export const routes: Routes = [
  {
    path: '',
    pathMatch: 'full',
    loadComponent: () =>
      import('./features/dashboard/dashboard.component').then(
        (m) => m.DashboardComponent,
      ),
    title: 'Dashboard · chaos',
  },
  {
    path: 'experiments',
    loadComponent: () =>
      import('./features/experiments/experiments-list/experiments-list.component').then(
        (m) => m.ExperimentsListComponent,
      ),
    title: 'Experiments · chaos',
  },
  {
    path: 'experiments/:id',
    loadComponent: () =>
      import('./features/experiments/experiment-detail/experiment-detail.component').then(
        (m) => m.ExperimentDetailComponent,
      ),
    title: 'Experiment · chaos',
  },
  {
    path: 'llm',
    loadComponent: () =>
      import('./features/llm/llm.component').then((m) => m.LlmComponent),
    title: 'LLM telemetry · chaos',
  },
  {
    path: 'findings',
    loadComponent: () =>
      import('./features/findings/findings.component').then(
        (m) => m.FindingsComponent,
      ),
    title: 'Findings · chaos',
  },
  {
    path: 'fixes',
    loadComponent: () =>
      import('./features/fixes/fixes.component').then((m) => m.FixesComponent),
    title: 'Fixes · chaos',
  },
  {
    // Catch-all → dashboard. Cheap UX for typos / stale links.
    path: '**',
    redirectTo: '',
  },
];
