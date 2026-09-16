import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import {
  CostAssumptions,
  EstimateCatalog,
  EstimateProgress,
  EstimateRecord,
  EstimateSummary,
  Estimation,
  ProjectInput,
  ProjectType,
} from './models';

/**
 * The Estimate module's API surface.
 *
 * Its own service rather than another section of `CompassApiService`, for the
 * same reason its types are its own file: the module has a flag, and a module
 * that can be switched off should take its whole surface with it. The shared
 * service is where a call belongs when two sections both make it; nothing else
 * calls these.
 *
 * The idioms are the shared service's, deliberately — `HttpClient` +
 * `firstValueFrom` for request/response, raw `fetch` for the stream, because
 * `HttpClient` buffers a response body and `EventSource` cannot POST. Same
 * shape, same failure handling, different file.
 */
@Injectable({ providedIn: 'root' })
export class EstimateApi {
  private readonly http = inject(HttpClient);

  catalog(): Promise<EstimateCatalog> {
    return firstValueFrom(this.http.get<EstimateCatalog>('/v1/estimates/catalog'));
  }

  list(): Promise<{ estimates: EstimateSummary[] }> {
    return firstValueFrom(
      this.http.get<{ estimates: EstimateSummary[] }>('/v1/estimates'),
    );
  }

  get(id: string): Promise<EstimateRecord> {
    return firstValueFrom(this.http.get<EstimateRecord>(`/v1/estimates/${id}`));
  }

  rateCard(): Promise<{ rate_card: CostAssumptions; baseline: CostAssumptions }> {
    return firstValueFrom(
      this.http.get<{ rate_card: CostAssumptions; baseline: CostAssumptions }>(
        '/v1/estimates/rate-card'),
    );
  }

  saveRateCard(card: CostAssumptions): Promise<{ rate_card: CostAssumptions }> {
    return firstValueFrom(
      this.http.put<{ rate_card: CostAssumptions }>('/v1/estimates/rate-card', card),
    );
  }

  /** Cost a brief without keeping it — the live rail beside the form.
   *  The same engine as the real estimate, with the two model stages taken at
   *  their deterministic fallback so a sidebar does not wait on a ReAct call. */
  preview(brief: ProjectInput): Promise<{ estimation: Estimation }> {
    return firstValueFrom(
      this.http.post<{ estimation: Estimation }>('/v1/estimates/preview', brief),
    );
  }

  /** Turn a paragraph into a brief. Returns the brief only — nothing is
   *  costed and nothing is stored until the person has read it. */
  draft(description: string, project_type: ProjectType): Promise<{ brief: ProjectInput }> {
    return firstValueFrom(
      this.http.post<{ brief: ProjectInput }>('/v1/estimates/draft',
        { description, project_type }),
    );
  }

  /** Read an uploaded requirements document and draft the brief from it.
   *  Returns the brief — carrying what the reading found — and nothing else:
   *  nothing costed, nothing stored. */
  readBrd(
    file: { name: string; mime: string; data_url: string },
    project_type: ProjectType,
  ): Promise<{ brief: ProjectInput; characters: number }> {
    return firstValueFrom(
      this.http.post<{ brief: ProjectInput; characters: number }>('/v1/estimates/brd',
        { ...file, project_type }),
    );
  }

  remove(id: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(
      this.http.delete<{ deleted: boolean }>(`/v1/estimates/${id}`),
    );
  }

  /**
   * Estimate a brief, reporting each stage as it starts.
   *
   * The progress is real: the server walks the stages and sends a frame when
   * one begins. So a run that stalls stalls visibly, on the named stage —
   * which is the entire reason for preferring this to the plain POST.
   */
  async stream(
    brief: ProjectInput,
    onProgress: (frame: EstimateProgress) => void,
  ): Promise<EstimateRecord> {
    const res = await fetch('/v1/estimates/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(brief),
    });
    if (!res.ok || !res.body) {
      // Carry the status: "could not reach the service" is a poor description
      // of a 404 from a service that answered.
      throw new Error(`${res.status} ${(await res.text()) || res.statusText}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let record: EstimateRecord | null = null;
    let failure = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (!frame.startsWith('data: ')) continue;
        let parsed: EstimateProgress;
        try {
          parsed = JSON.parse(frame.slice(6));
        } catch {
          continue; // a truncated frame is not worth failing a run over
        }
        onProgress(parsed);
        if (parsed.status === 'failed') failure = parsed.error || 'estimate failed';
        if (parsed.estimate) record = parsed.estimate;
      }
    }

    if (failure) throw new Error(failure);
    if (!record) {
      // The stream ended without the estimate — a dropped connection, or a
      // proxy that closed it. Say that, rather than rendering an empty report.
      throw new Error('the estimate stream ended before the result arrived');
    }
    return record;
  }
}
