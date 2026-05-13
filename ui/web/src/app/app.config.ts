import {
  ApplicationConfig,
  provideBrowserGlobalErrorListeners,
  provideZonelessChangeDetection,
} from '@angular/core';
import { provideHttpClient, withFetch } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { provideEchartsCore } from 'ngx-echarts';

import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    // Zoneless change detection — Angular 21 default for new apps; keeps
    // bundle smaller and lets us drive updates from Signals without zone.js.
    provideZonelessChangeDetection(),
    provideRouter(routes, withComponentInputBinding()),
    // withFetch enables HTTP/2 + service-worker compatibility; the underlying
    // implementation is the platform fetch instead of XHR.
    provideHttpClient(withFetch()),
    // Material components rely on @angular/animations.
    provideAnimations(),
    // ECharts loaded lazily on first use so the initial bundle stays small.
    provideEchartsCore({ echarts: () => import('echarts') }),
  ],
};
