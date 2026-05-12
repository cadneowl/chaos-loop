import { Routes } from '@angular/router';

/**
 * Lazy-loaded route tree. Each feature is its own bundle; the dashboard is
 * the default route.
 */
export const routes: Routes = [
  {
    path: '',
    pathMatch: 'full',
    redirectTo: 'experiments',
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
    // Catch-all → back to the list. Cheap UX for typos / stale links.
    path: '**',
    redirectTo: 'experiments',
  },
];
