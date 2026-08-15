import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { AuthService } from './auth.service';
import {
  BackgroundTask,
  BackgroundTasksResponse,
  ChatAttachment,
  ChatCard,
  CompassEvent,
  GitStatus,
  GithubRepo,
  HealthInfo,
  CustomizeInfo,
  DesignClarify,
  DesignFile,
  DesignPage,
  DesignProject,
  DesignSystem,
  DesignSystemDoc,
  DesignTemplate,
  DesignTurn,
  DesignVersion,
  FileEntry,
  FileHit,
  MemoryEntry,
  Recap,
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
  backgroundTaskLogs(id: string): Promise<{ lines: string[] }> {
    return firstValueFrom(
      this.http.get<{ lines: string[] }>(`/v1/background-tasks/${id}/logs`),
    );
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

  /** Expressive TTS — returns an mp3 blob. Raw fetch (binary comes back
   * untouched); the auth cookie rides along via credentials. Throws on 503
   * (TTS not deployed) so the caller can fall back to the browser voice. */
  async synthesizeSpeech(text: string, voice?: string): Promise<Blob> {
    const res = await fetch('/v1/speech', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'include',
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
    workIq = false,
  ): Promise<void> {
    return this.streamPost(
      `/v1/chat/sessions/${sessionId}/messages`,
      { content, attachments, work_iq: workIq },
      onEvent,
    );
  }

  async streamChatRegenerate(
    sessionId: string,
    onEvent: (event: CompassEvent) => void,
    workIq = false,
  ): Promise<void> {
    return this.streamPost(
      `/v1/chat/sessions/${sessionId}/regenerate`,
      { work_iq: workIq },
      onEvent,
    );
  }

  async streamChatEdit(
    sessionId: string,
    index: number,
    content: string,
    onEvent: (event: CompassEvent) => void,
    workIq = false,
  ): Promise<void> {
    return this.streamPost(
      `/v1/chat/sessions/${sessionId}/edit`,
      { index, content, work_iq: workIq },
      onEvent,
    );
  }

  workIqStatus(): Promise<{ configured: boolean }> {
    return firstValueFrom(
      this.http.get<{ configured: boolean }>('/v1/chat/work-iq'),
    );
  }

  // -- realtime voice mode (Azure OpenAI Realtime / WebRTC) -----------------
  voiceStatus(): Promise<{ available: boolean }> {
    return firstValueFrom(this.http.get<{ available: boolean }>('/v1/chat/voice'));
  }
  voiceSession(): Promise<{ token: string; webrtc_url: string }> {
    return firstValueFrom(
      this.http.post<{ token: string; webrtc_url: string }>('/v1/chat/voice/session', {}),
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

  listChatSessions(): Promise<{ sessions: ChatCard[] }> {
    return firstValueFrom(
      this.http.get<{ sessions: ChatCard[] }>('/v1/chat/sessions'),
    );
  }

  deleteChatSession(sessionId: string): Promise<{ deleted: string }> {
    return firstValueFrom(
      this.http.delete<{ deleted: string }>(`/v1/chat/sessions/${sessionId}`),
    );
  }

  suggestNext(sessionId: string): Promise<{ suggestion: string }> {
    return firstValueFrom(
      this.http.post<{ suggestion: string }>(`/v1/sessions/${sessionId}/suggest`, {}),
    );
  }

  // -- Files browser --------------------------------------------------------
  listFiles(ws: string, path: string): Promise<{ entries: FileEntry[] }> {
    return firstValueFrom(
      this.http.get<{ entries: FileEntry[] }>(
        `/v1/workspaces/${ws}/files?path=${encodeURIComponent(path)}`),
    );
  }
  readFile(ws: string, path: string): Promise<{ content: string }> {
    return firstValueFrom(
      this.http.get<{ content: string }>(
        `/v1/workspaces/${ws}/file?path=${encodeURIComponent(path)}`),
    );
  }
  searchFiles(ws: string, q: string, content: boolean): Promise<{ hits: FileHit[] }> {
    return firstValueFrom(
      this.http.get<{ hits: FileHit[] }>(
        `/v1/workspaces/${ws}/files/search?q=${encodeURIComponent(q)}&content=${content}`),
    );
  }

  // -- Design ---------------------------------------------------------------
  designTemplates(): Promise<{ templates: DesignTemplate[] }> {
    return firstValueFrom(
      this.http.get<{ templates: DesignTemplate[] }>('/v1/design/templates'),
    );
  }
  designProjects(): Promise<{ projects: DesignProject[] }> {
    return firstValueFrom(
      this.http.get<{ projects: DesignProject[] }>('/v1/design/projects'),
    );
  }
  designProject(id: string): Promise<DesignProject> {
    return firstValueFrom(this.http.get<DesignProject>(`/v1/design/projects/${id}`));
  }
  createDesign(body: {
    name?: string;
    template?: string;
    prompt?: string;
    design_system?: string;
    design_systems?: string[];
  }): Promise<DesignProject> {
    return firstValueFrom(this.http.post<DesignProject>('/v1/design/projects', body));
  }
  patchDesign(
    id: string,
    patch: {
      name?: string;
      html?: string;
      starred?: boolean;
      design_system?: string;
      design_systems?: string[];
      turns?: DesignTurn[];
      clarify?: DesignClarify | Record<string, never>;
    },
  ): Promise<DesignProject> {
    return firstValueFrom(this.http.patch<DesignProject>(`/v1/design/projects/${id}`, patch));
  }
  deleteDesign(id: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(this.http.delete<{ deleted: boolean }>(`/v1/design/projects/${id}`));
  }
  designSystems(): Promise<{ systems: DesignSystem[]; included: DesignSystem[] }> {
    return firstValueFrom(
      this.http.get<{ systems: DesignSystem[]; included: DesignSystem[] }>(
        '/v1/design/systems',
      ),
    );
  }
  createDesignSystem(body: {
    name?: string;
    source?: string;
    text?: string;
    css?: string;
    url?: string;
    workspace_id?: string;
    path?: string;
  }): Promise<DesignSystem> {
    return firstValueFrom(this.http.post<DesignSystem>('/v1/design/systems', body));
  }

  // -- a project's canvas, history, and pins
  saveDesignHtml(id: string, html: string, label = 'Edited on canvas'): Promise<DesignProject> {
    return firstValueFrom(
      this.http.post<DesignProject>(`/v1/design/projects/${id}/html`, { html, label }),
    );
  }
  openDesign(id: string): Promise<DesignProject> {
    return firstValueFrom(this.http.post<DesignProject>(`/v1/design/projects/${id}/open`, {}));
  }
  duplicateDesign(id: string): Promise<DesignProject> {
    return firstValueFrom(
      this.http.post<DesignProject>(`/v1/design/projects/${id}/duplicate`, {}),
    );
  }
  /** Read an attachment server-side: PDFs, Word files and zips come back as
   *  text, images come back as themselves. */
  attachForDesign(file: { name: string; mime: string; data_url: string }): Promise<{
    kind: 'text' | 'image';
    name: string;
    text?: string;
    data_url?: string;
  }> {
    return firstValueFrom(
      this.http.post<{ kind: 'text' | 'image'; name: string; text?: string; data_url?: string }>(
        '/v1/design/attach',
        file,
      ),
    );
  }

  /** Ask whether a brief is specific enough, and what to ask if it isn't. */
  clarifyDesign(
    prompt: string,
    template: string,
    opts: { answers?: string; followup?: boolean } = {},
  ): Promise<DesignClarify> {
    return firstValueFrom(
      this.http.post<DesignClarify>('/v1/design/clarify', {
        prompt,
        template,
        answers: opts.answers ?? '',
        followup: opts.followup ?? false,
      }),
    );
  }

  // -- pages
  designPages(id: string): Promise<{ pages: DesignPage[]; active: string }> {
    return firstValueFrom(
      this.http.get<{ pages: DesignPage[]; active: string }>(
        `/v1/design/projects/${id}/pages`,
      ),
    );
  }
  addDesignPage(id: string, name = ''): Promise<DesignProject> {
    return firstValueFrom(
      this.http.post<DesignProject>(`/v1/design/projects/${id}/pages`, { name }),
    );
  }
  deleteDesignPage(id: string, pageId: string): Promise<DesignProject> {
    return firstValueFrom(
      this.http.delete<DesignProject>(`/v1/design/projects/${id}/pages/${pageId}`),
    );
  }
  openDesignPage(id: string, pageId: string): Promise<DesignProject> {
    return firstValueFrom(
      this.http.post<DesignProject>(`/v1/design/projects/${id}/pages/${pageId}/open`, {}),
    );
  }

  // -- a project's own files
  designFiles(
    id: string,
    path = '',
  ): Promise<{ path: string; folders: DesignFile[]; files: DesignFile[] }> {
    return firstValueFrom(
      this.http.get<{ path: string; folders: DesignFile[]; files: DesignFile[] }>(
        `/v1/design/projects/${id}/files?path=${encodeURIComponent(path)}`,
      ),
    );
  }
  designFileUrl(id: string, path: string): string {
    return `/v1/design/projects/${id}/files/read?path=${encodeURIComponent(path)}`;
  }
  designFileText(id: string, path: string): Promise<string> {
    return firstValueFrom(
      this.http.get(this.designFileUrl(id, path), { responseType: 'text' }),
    );
  }
  writeDesignFile(
    id: string,
    body: { path: string; text?: string; data_url?: string },
  ): Promise<DesignFile> {
    return firstValueFrom(
      this.http.post<DesignFile>(`/v1/design/projects/${id}/files`, body),
    );
  }
  deleteDesignFile(id: string, path: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(
      this.http.delete<{ deleted: boolean }>(
        `/v1/design/projects/${id}/files?path=${encodeURIComponent(path)}`,
      ),
    );
  }

  designVersions(
    id: string,
  ): Promise<{ current: { label: string; at: number }; versions: DesignVersion[] }> {
    return firstValueFrom(
      this.http.get<{ current: { label: string; at: number }; versions: DesignVersion[] }>(
        `/v1/design/projects/${id}/versions`,
      ),
    );
  }
  restoreDesignVersion(id: string, versionId: string): Promise<DesignProject> {
    return firstValueFrom(
      this.http.post<DesignProject>(
        `/v1/design/projects/${id}/versions/${versionId}/restore`,
        {},
      ),
    );
  }
  addDesignComment(
    id: string,
    body: { x: number; y: number; text: string },
  ): Promise<DesignProject> {
    return firstValueFrom(
      this.http.post<DesignProject>(`/v1/design/projects/${id}/comments`, body),
    );
  }
  deleteDesignComment(id: string, commentId: string): Promise<DesignProject> {
    return firstValueFrom(
      this.http.delete<DesignProject>(`/v1/design/projects/${id}/comments/${commentId}`),
    );
  }
  /** Cache-busted by updated_at so a re-render replaces the stale thumbnail. */
  designThumbUrl(id: string, updatedAt: number): string {
    return `/v1/design/projects/${id}/thumbnail?v=${Math.floor(updatedAt)}`;
  }
  deleteDesignSystem(id: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(this.http.delete<{ deleted: boolean }>(`/v1/design/systems/${id}`));
  }
  // -- a design system as a project
  designSystemDoc(id: string): Promise<DesignSystemDoc> {
    return firstValueFrom(
      this.http.get<DesignSystemDoc>(`/v1/design/systems/${id}/doc`),
    );
  }
  /** A section's page, loaded straight into a preview frame. */
  designSystemPageUrl(id: string, sectionId: string): string {
    return `/v1/design/systems/${id}/page/${sectionId}`;
  }
  designSystemPage(id: string, sectionId: string): Promise<string> {
    return firstValueFrom(
      this.http.get(this.designSystemPageUrl(id, sectionId), { responseType: 'text' }),
    );
  }
  designSystemFileUrl(id: string, path: string): string {
    return `/v1/design/systems/${id}/file?path=${encodeURIComponent(path)}`;
  }
  /** Build a system from the set-up form. */
  setUpDesignSystem(body: {
    name?: string;
    blurb?: string;
    github?: string;
    workspace_id?: string;
    path?: string;
    files?: Array<{ name: string; text: string }>;
    images?: string[];
    notes?: string;
    css?: string;
  }): Promise<DesignSystem> {
    return firstValueFrom(this.http.post<DesignSystem>('/v1/design/systems/setup', body));
  }
  duplicateDesignSystem(id: string): Promise<DesignSystem> {
    return firstValueFrom(
      this.http.post<DesignSystem>(`/v1/design/systems/${id}/duplicate`, {}),
    );
  }
  designSystemExportUrl(id: string): string {
    return `/v1/design/systems/${id}/export`;
  }
  designSystemFile(id: string, path: string): Promise<string> {
    return firstValueFrom(
      this.http.get(this.designSystemFileUrl(id, path), { responseType: 'text' }),
    );
  }
  saveSystemUsage(
    id: string,
    section: string,
    note: string,
  ): Promise<{ usage: Record<string, string> }> {
    return firstValueFrom(
      this.http.post<{ usage: Record<string, string> }>(
        `/v1/design/systems/${id}/usage`,
        { section, note },
      ),
    );
  }

  designExportUrl(id: string, format: string): string {
    return `/v1/design/projects/${id}/export?format=${format}`;
  }

  /** Fetch an export as a blob. Going through the API rather than pointing an
   *  anchor at the URL means a 501 or a 502 surfaces as an error the panel can
   *  show, instead of a download that silently never starts. */
  async downloadBlob(url: string): Promise<Blob> {
    const response = await fetch(url, { credentials: 'same-origin' });
    if (!response.ok) {
      let detail = `${response.status}`;
      try {
        detail = (await response.json()).detail ?? detail;
      } catch {
        /* not JSON — keep the status */
      }
      throw new Error(detail);
    }
    return response.blob();
  }

  /** Generate or refine a project's design. Slow — a whole document is written. */
  generateDesign(
    id: string,
    prompt: string,
    model = '',
    images: string[] = [],
  ): Promise<DesignProject> {
    return firstValueFrom(
      this.http.post<DesignProject>(`/v1/design/projects/${id}/generate`, {
        prompt,
        model,
        images,
      }),
    );
  }

  customize(): Promise<CustomizeInfo> {
    return firstValueFrom(this.http.get<CustomizeInfo>('/v1/customize'));
  }

  recap(days = 30): Promise<Recap> {
    return firstValueFrom(this.http.get<Recap>(`/v1/recap?days=${days}`));
  }

  // -- memory (Settings → Memory) -------------------------------------------
  listMemory(scope?: string): Promise<{ entries: MemoryEntry[]; categories: string[] }> {
    const q = scope ? `?scope=${encodeURIComponent(scope)}` : '';
    return firstValueFrom(
      this.http.get<{ entries: MemoryEntry[]; categories: string[] }>('/v1/memory' + q),
    );
  }
  patchMemory(
    id: string,
    patch: { summary?: string; details?: string; category?: string },
  ): Promise<MemoryEntry> {
    return firstValueFrom(this.http.patch<MemoryEntry>(`/v1/memory/${id}`, patch));
  }
  deleteMemory(id: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(this.http.delete<{ deleted: boolean }>(`/v1/memory/${id}`));
  }

  forkChatSession(sessionId: string, index?: number): Promise<{ session_id: string }> {
    return firstValueFrom(
      this.http.post<{ session_id: string }>(`/v1/chat/sessions/${sessionId}/fork`, {
        index: index ?? null,
      }),
    );
  }

  patchChatSession(
    sessionId: string,
    patch: { title?: string; pinned?: boolean },
  ): Promise<{ ok: boolean }> {
    return firstValueFrom(
      this.http.patch<{ ok: boolean }>(`/v1/chat/sessions/${sessionId}`, patch),
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
    // Raw fetch (EventSource can't POST, HttpClient buffers) — the auth cookie
    // rides along via credentials; the interceptor can't see this call.
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      credentials: 'include',
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
