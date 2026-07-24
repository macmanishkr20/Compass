import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { AuthService } from './auth.service';
import {
  BackgroundTask,
  BackgroundTasksResponse,
  ChatAttachment,
  CompassEvent,
  GitStatus,
  GithubRepo,
  HealthInfo,
  PermissionBehavior,
  Routine,
  RoutineRun,
  RoutinesResponse,
  SessionCard,
  Workspace,
} from './models';

interface CreateSessionResponse {
  session_id: string;
  resumed_messages: number;
}
interface TranscriptResponse {
  session_id: string;
  messages: Array<{
    uuid: string;
    role: string;
    content: string | null;
    timestamp?: number; // epoch seconds
    meta?: Record<string, unknown>;
  }>;
}

@Injectable({ providedIn: 'root' })
export class CompassApiService {
  private readonly http = inject(HttpClient);
  private readonly auth = inject(AuthService);

  health(): Promise<HealthInfo> {
    return firstValueFrom(this.http.get<HealthInfo>('/healthz'));
  }

  listSessions(): Promise<{ sessions: SessionCard[] }> {
    return firstValueFrom(
      this.http.get<{ sessions: SessionCard[] }>('/v1/sessions'),
    );
  }

  updateSession(
    sessionId: string,
    patch: Partial<
      Pick<
        SessionCard,
        | 'title'
        | 'pinned'
        | 'archived'
        | 'group'
        | 'mode'
        | 'effort'
        | 'model'
        | 'workspace'
      >
    >,
  ): Promise<SessionCard> {
    return firstValueFrom(
      this.http.patch<SessionCard>(`/v1/sessions/${sessionId}`, patch),
    );
  }

  deleteSession(sessionId: string): Promise<unknown> {
    return firstValueFrom(this.http.delete(`/v1/sessions/${sessionId}`));
  }

  forkSession(sessionId: string, upToUuid?: string): Promise<{ session_id: string }> {
    return firstValueFrom(
      this.http.post<{ session_id: string }>(`/v1/sessions/${sessionId}/fork`, {
        up_to_uuid: upToUuid ?? null,
      }),
    );
  }

  createSession(opts: {
    resume?: boolean;
    sessionId?: string;
    permissionMode?: string;
    effort?: string;
    model?: string;
    workspaceId?: string;
  } = {}): Promise<CreateSessionResponse> {
    return firstValueFrom(
      this.http.post<CreateSessionResponse>('/v1/sessions', {
        resume: opts.resume ?? false,
        session_id: opts.sessionId,
        permission_mode: opts.permissionMode,
        effort: opts.effort,
        model: opts.model,
        workspace_id: opts.workspaceId,
      }),
    );
  }

  listWorkspaces(): Promise<{ workspaces: Workspace[] }> {
    return firstValueFrom(
      this.http.get<{ workspaces: Workspace[] }>('/v1/workspaces'),
    );
  }

  addFolderWorkspace(body: { path?: string; name?: string }): Promise<Workspace> {
    return firstValueFrom(this.http.post<Workspace>('/v1/workspaces/folder', body));
  }

  deleteWorkspace(id: string): Promise<unknown> {
    return firstValueFrom(this.http.delete(`/v1/workspaces/${id}`));
  }

  pickFolder(): Promise<{ path: string }> {
    return firstValueFrom(this.http.post<{ path: string }>('/v1/pick-folder', {}));
  }
  revealWorkspace(id: string): Promise<{ opened: string }> {
    return firstValueFrom(this.http.post<{ opened: string }>(`/v1/workspaces/${id}/reveal`, {}));
  }

  openWorkspaceTerminal(id: string): Promise<{ opened: string }> {
    return firstValueFrom(this.http.post<{ opened: string }>(`/v1/workspaces/${id}/terminal`, {}));
  }

  openWorkspaceInVsCode(id: string): Promise<{ opened: string; command: string }> {
    return firstValueFrom(
      this.http.post<{ opened: string; command: string }>(
        `/v1/workspaces/${id}/open-in-vscode`,
        {},
      ),
    );
  }

  gitStatus(id: string): Promise<GitStatus> {
    return firstValueFrom(this.http.get<GitStatus>(`/v1/workspaces/${id}/git`));
  }

  gitDiff(id: string): Promise<{ diff: string }> {
    return firstValueFrom(this.http.get<{ diff: string }>(`/v1/workspaces/${id}/diff`));
  }

  screenshot(url: string, fullPage = false): Promise<{ image: string }> {
    return firstValueFrom(
      this.http.post<{ image: string }>('/v1/screenshot', { url, full_page: fullPage }),
    );
  }

  createPr(
    id: string,
    opts: { draft?: boolean; manual?: boolean } = {},
  ): Promise<{ url: string; branch: string; existing: boolean; manual?: boolean }> {
    return firstValueFrom(
      this.http.post<{ url: string; branch: string; existing: boolean; manual?: boolean }>(
        `/v1/workspaces/${id}/pr`,
        opts,
      ),
    );
  }

  githubRepos(): Promise<{ repos: GithubRepo[] }> {
    return firstValueFrom(this.http.get<{ repos: GithubRepo[] }>('/v1/github/repos'));
  }

  // -- background tasks -----------------------------------------------------
  backgroundTasks(): Promise<BackgroundTasksResponse> {
    return firstValueFrom(this.http.get<BackgroundTasksResponse>('/v1/background-tasks'));
  }
  stopBackgroundTask(id: string): Promise<{ stopped: boolean }> {
    return firstValueFrom(
      this.http.post<{ stopped: boolean }>(`/v1/background-tasks/${id}/stop`, {}),
    );
  }
  clearBackgroundTasks(): Promise<{ cleared: number }> {
    return firstValueFrom(
      this.http.post<{ cleared: number }>('/v1/background-tasks/clear', {}),
    );
  }

  // -- routines -------------------------------------------------------------
  routines(): Promise<RoutinesResponse> {
    return firstValueFrom(this.http.get<RoutinesResponse>('/v1/routines'));
  }
  getRoutine(id: string): Promise<Routine> {
    return firstValueFrom(this.http.get<Routine>(`/v1/routines/${id}`));
  }
  createRoutine(body: Partial<Routine>): Promise<Routine> {
    return firstValueFrom(this.http.post<Routine>('/v1/routines', body));
  }
  updateRoutine(id: string, patch: Partial<Routine>): Promise<Routine> {
    return firstValueFrom(this.http.patch<Routine>(`/v1/routines/${id}`, patch));
  }
  deleteRoutine(id: string): Promise<{ deleted: string }> {
    return firstValueFrom(this.http.delete<{ deleted: string }>(`/v1/routines/${id}`));
  }
  runRoutineNow(id: string): Promise<RoutineRun> {
    return firstValueFrom(this.http.post<RoutineRun>(`/v1/routines/${id}/run`, {}));
  }
  routineRuns(id: string): Promise<{ runs: RoutineRun[] }> {
    return firstValueFrom(this.http.get<{ runs: RoutineRun[] }>(`/v1/routines/${id}/runs`));
  }
  recentRoutineRuns(
    since: number,
  ): Promise<{ runs: (RoutineRun & { notify_enabled: boolean; notify_push: boolean; notify_email: boolean })[]; email_configured: boolean }> {
    return firstValueFrom(
      this.http.get<{ runs: any[]; email_configured: boolean }>(
        `/v1/routine-runs/recent?since=${since}`,
      ),
    );
  }

  githubClone(fullName: string, branch?: string): Promise<Workspace> {
    return firstValueFrom(
      this.http.post<Workspace>('/v1/github/clone', {
        full_name: fullName,
        branch: branch ?? null,
      }),
    );
  }

  /** Expressive TTS — returns an mp3 blob. Raw fetch so the token is attached
   * and binary comes back untouched. Throws on 503 (TTS not deployed) so the
   * caller can fall back to the browser voice. */
  async synthesizeSpeech(text: string, voice?: string): Promise<Blob> {
    const headers: Record<string, string> = { 'content-type': 'application/json' };
    const token = this.auth.token;
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch('/v1/speech', {
      method: 'POST',
      headers,
      body: JSON.stringify({ text, voice: voice ?? null }),
    });
    if (!res.ok) {
      throw new Error((await res.text()) || res.statusText);
    }
    return res.blob();
  }

  transcript(sessionId: string): Promise<TranscriptResponse> {
    return firstValueFrom(
      this.http.get<TranscriptResponse>(
        `/v1/sessions/${sessionId}/transcript`,
      ),
    );
  }

  // -- Home/Chat (separate, tool-free workflow: /v1/chat/*) -----------------
  createChatSession(opts: {
    resume?: boolean;
    sessionId?: string;
    effort?: string;
    model?: string;
  } = {}): Promise<CreateSessionResponse> {
    return firstValueFrom(
      this.http.post<CreateSessionResponse>('/v1/chat/sessions', {
        resume: opts.resume ?? false,
        session_id: opts.sessionId,
        effort: opts.effort,
        model: opts.model,
      }),
    );
  }

  async streamChatMessage(
    sessionId: string,
    content: string,
    onEvent: (event: CompassEvent) => void,
    attachments: ChatAttachment[] = [],
  ): Promise<void> {
    return this.streamPost(
      `/v1/chat/sessions/${sessionId}/messages`,
      { content, attachments },
      onEvent,
    );
  }

  abortChat(sessionId: string): Promise<unknown> {
    return firstValueFrom(this.http.post(`/v1/chat/sessions/${sessionId}/abort`, {}));
  }

  chatTranscript(sessionId: string): Promise<TranscriptResponse> {
    return firstValueFrom(
      this.http.get<TranscriptResponse>(`/v1/chat/sessions/${sessionId}/transcript`),
    );
  }

  resolvePermission(
    sessionId: string,
    requestId: string,
    behavior: Exclude<PermissionBehavior, 'timeout'>,
  ): Promise<unknown> {
    return firstValueFrom(
      this.http.post(
        `/v1/sessions/${sessionId}/permissions/${requestId}`,
        { behavior },
      ),
    );
  }

  abort(sessionId: string): Promise<unknown> {
    return firstValueFrom(
      this.http.post(`/v1/sessions/${sessionId}/abort`, {}),
    );
  }

  /**
   * Send a message and stream the SSE response. Each parsed CompassEvent is
   * delivered to `onEvent`. The returned AbortController lets the caller stop
   * reading (the turn itself is stopped via abort()). Uses fetch streaming —
   * EventSource can't POST.
   */
  async streamMessage(
    sessionId: string,
    content: string,
    onEvent: (event: CompassEvent) => void,
    attachments: ChatAttachment[] = [],
  ): Promise<void> {
    return this.streamPost(
      `/v1/sessions/${sessionId}/messages`,
      { content, attachments },
      onEvent,
    );
  }

  /** Edit a past user prompt and re-run from that checkpoint. */
  async streamEdit(
    sessionId: string,
    messageUuid: string,
    content: string,
    onEvent: (event: CompassEvent) => void,
  ): Promise<void> {
    return this.streamPost(
      `/v1/sessions/${sessionId}/messages/${messageUuid}/edit`,
      { content },
      onEvent,
    );
  }

  /** Re-run the last user turn, discarding the previous answer. */
  async streamRegenerate(
    sessionId: string,
    onEvent: (event: CompassEvent) => void,
  ): Promise<void> {
    return this.streamPost(`/v1/sessions/${sessionId}/regenerate`, {}, onEvent);
  }

  private async streamPost(
    url: string,
    body: unknown,
    onEvent: (event: CompassEvent) => void,
  ): Promise<void> {
    // Raw fetch (EventSource can't POST, HttpClient buffers) — so the bearer
    // token is attached here directly; the interceptor can't see this call.
    const headers: Record<string, string> = {
      'content-type': 'application/json',
    };
    const token = this.auth.token;
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      this.auth.sessionExpired();
      throw new Error('authentication required');
    }
    if (!res.ok || !res.body) {
      throw new Error((await res.text()) || res.statusText);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const event = this.parseFrame(frame);
        if (event) onEvent(event);
      }
    }
  }

  private parseFrame(frame: string): CompassEvent | null {
    let type = '';
    let data = '';
    for (const line of frame.split('\n')) {
      if (line.startsWith('event: ')) type = line.slice(7).trim();
      else if (line.startsWith('data: ')) data += line.slice(6);
    }
    if (!type || !data) return null;
    try {
      return { ...(JSON.parse(data) as object), type } as CompassEvent;
    } catch {
      return null;
    }
  }
}
