import { Injectable, inject } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';

import type {
  ControlSignals,
  ExperimentListResponse,
  ExperimentRecord,
  ExperimentState,
} from './contracts';

/**
 * HTTP client for the chaos UI server.
 *
 * Base URL defaults to relative `/api/v1` so the same build works whether
 * served by the Nest server (production) or by `ng serve` proxied to it
 * (development; see `proxy.conf.json`).
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly base = '/api/v1';

  /** GET /experiments?state=&target_app=&from=&to=&limit=&offset= */
  listExperiments(opts: ListOptions = {}): Observable<ExperimentListResponse> {
    let params = new HttpParams();
    for (const [key, value] of Object.entries(opts)) {
      if (value !== undefined && value !== null && value !== '') {
        params = params.set(key, String(value));
      }
    }
    return this.http.get<ExperimentListResponse>(`${this.base}/experiments`, { params });
  }

  /** GET /experiments/:id */
  getExperiment(id: string): Observable<ExperimentRecord> {
    return this.http.get<ExperimentRecord>(`${this.base}/experiments/${id}`);
  }

  /** GET /experiments/:id/control */
  getControl(id: string): Observable<ControlSignals> {
    return this.http.get<ControlSignals>(`${this.base}/experiments/${id}/control`);
  }
}

export interface ListOptions {
  state?: ExperimentState;
  target_app?: string;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
}
