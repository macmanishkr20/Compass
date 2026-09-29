import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { AuthService } from './auth.service';
import {
  MissionDetail,
  MissionSummary,
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
  NodeTypeInfo,
  ConnectionKind,
  CredentialTypeInfo,
  PipelineConnection,
  PipelineExport,
  PipelineProblem,
  PipelineRun,
  PipelineSummary,
  Recap,
  PermissionBehavior,
  Routine,
  RoutineRun,
  RoutinesResponse,
  SavedPrompt,
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
    /** The calls an assistant turn made. Present in the payload all along and
     *  simply not declared here, which is why a reopened session used to show
     *  the thinking and the prose and none of the work. */
    tool_calls?: Array<{
      id: string;
      type?: string;
      function?: { name?: string; arguments?: string };
    }>;
    /** On a `tool` message: which call this is the result of. */
    tool_call_id?: string;
    is_error?: boolean;
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

  // -- Pipelines. These endpoints exist only when the module is enabled, so
  // they are called from the section itself rather than at startup.
  pipelines(): Promise<{ pipelines: PipelineSummary[] }> {
    return firstValueFrom(
      this.http.get<{ pipelines: PipelineSummary[] }>('/v1/pipelines'),
    );
  }
  pipelineNodeTypes(): Promise<{
    node_types: NodeTypeInfo[];
    connection_kinds: ConnectionKind[];
  }> {
    return firstValueFrom(
      this.http.get<{ node_types: NodeTypeInfo[]; connection_kinds: ConnectionKind[] }>(
        '/v1/pipelines/node-types',
      ),
    );
  }
  createPipeline(name: string): Promise<PipelineSummary> {
    return firstValueFrom(
      this.http.post<PipelineSummary>('/v1/pipelines', { name }),
    );
  }
  getPipeline(id: string): Promise<PipelineSummary> {
    return firstValueFrom(this.http.get<PipelineSummary>(`/v1/pipelines/${id}`));
  }
  savePipeline(id: string, patch: Partial<PipelineSummary>): Promise<PipelineSummary> {
    return firstValueFrom(
      this.http.patch<PipelineSummary>(`/v1/pipelines/${id}`, patch),
    );
  }
  deletePipeline(id: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(
      this.http.delete<{ deleted: boolean }>(`/v1/pipelines/${id}`),
    );
  }
  validatePipeline(id: string): Promise<{ ok: boolean; problems: PipelineProblem[] }> {
    return firstValueFrom(
      this.http.post<{ ok: boolean; problems: PipelineProblem[] }>(
        `/v1/pipelines/${id}/validate`, {},
      ),
    );
  }
  runPipeline(
    id: string,
    parameters: Record<string, unknown> = {},
    mode: 'live' | 'mock' = 'live',
  ): Promise<PipelineRun> {
    return firstValueFrom(
      this.http.post<PipelineRun>(`/v1/pipelines/${id}/run`, { parameters, mode }),
    );
  }
  /** Run one step on its own, with `seed` standing in for its upstream. */
  runPipelineNode(
    id: string,
    nodeId: string,
    seed: Record<string, unknown> = {},
    mode: 'live' | 'mock' = 'live',
  ): Promise<PipelineRun> {
    return firstValueFrom(
      this.http.post<PipelineRun>(
        `/v1/pipelines/${id}/nodes/${nodeId}/run`, { seed, mode },
      ),
    );
  }
  pipelineRuns(id: string): Promise<{ runs: PipelineRun[] }> {
    return firstValueFrom(
      this.http.get<{ runs: PipelineRun[] }>(`/v1/pipelines/${id}/runs`),
    );
  }
  resumePipelineRun(runId: string, answer: Record<string, unknown>): Promise<PipelineRun> {
    return firstValueFrom(
      this.http.post<PipelineRun>(`/v1/pipeline-runs/${runId}/resume`, { answer }),
    );
  }
  exportPipeline(id: string): Promise<PipelineExport> {
    return firstValueFrom(
      this.http.get<PipelineExport>(`/v1/pipelines/${id}/export`),
    );
  }
  pipelineConnections(): Promise<{ connections: PipelineConnection[] }> {
    return firstValueFrom(
      this.http.get<{ connections: PipelineConnection[] }>('/v1/pipeline-connections'),
    );
  }
  /** The secret travels once, on the way in. It is stored behind a reference
   *  and never comes back out of the API. */
  createConnection(body: {
    kind: string;
    name: string;
    auth: string;
    config: Record<string, unknown>;
    secret: string;
  }): Promise<PipelineConnection> {
    return firstValueFrom(
      this.http.post<PipelineConnection>('/v1/pipeline-connections', body),
    );
  }
  deleteConnection(id: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(
      this.http.delete<{ deleted: boolean }>(`/v1/pipeline-connections/${id}`),
    );
  }
  /** What each kind of connection needs, so the form is the credential's own
   *  rather than one opaque "secret" box for every service. */
  connectionTypes(): Promise<{ types: CredentialTypeInfo[] }> {
    return firstValueFrom(
      this.http.get<{ types: CredentialTypeInfo[] }>('/v1/pipeline-connection-types'),
    );
  }
  /** Edit in place: the graph points at the connection id, so rotating a
   *  token must not mean deleting and recreating it. */
  patchConnection(
    id: string,
    body: { name?: string; values?: Record<string, string> },
  ): Promise<PipelineConnection> {
    return firstValueFrom(
      this.http.patch<PipelineConnection>(`/v1/pipeline-connections/${id}`, body),
    );
  }
  /** Prove the credential now, against the provider. A failed test is an
   *  answer, not an error, so this resolves either way. */
  testConnection(id: string): Promise<{ ok: boolean; message: string }> {
    return firstValueFrom(
      this.http.post<{ ok: boolean; message: string }>(
        `/v1/pipeline-connections/${id}/test`, {}),
    );
  }
  signInConnection(id: string): Promise<{ url: string }> {
    return firstValueFrom(
      this.http.post<{ url: string }>(`/v1/pipeline-connections/${id}/sign-in`, {}),
    );
  }
  signOutConnection(id: string): Promise<PipelineConnection> {
    return firstValueFrom(
      this.http.delete<PipelineConnection>(`/v1/pipeline-connections/${id}/sign-in`),
    );
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

  /** Throw away every uncommitted change in a workspace. Irreversible — the
   *  `confirm` flag is required by the server, not decoration. */
  discardChanges(id: string): Promise<{ ok: boolean; discarded: number }> {
    return firstValueFrom(
      this.http.post<{ ok: boolean; discarded: number }>(
        `/v1/workspaces/${id}/discard`, { confirm: true },
      ),
    );
  }

  screenshot(url: string, fullPage = false): Promise<{ image: string }> {
    return firstValueFrom(
      this.http.post<{ image: string }>('/v1/screenshot', { url, full_page: fullPage }),
    );
  }

  createPr(
    id: string,
    opts: { draft?: boolean; manual?: boolean; title?: string; body?: string } = {},
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

  // -- the prompt library: things worth asking again ------------------------
  savedPrompts(): Promise<{ prompts: SavedPrompt[]; on_screen: number }> {
    return firstValueFrom(
      this.http.get<{ prompts: SavedPrompt[]; on_screen: number }>('/v1/chat/prompts'),
    );
  }

  savePrompt(title: string, text: string, sessionId = ''): Promise<SavedPrompt> {
    return firstValueFrom(
      this.http.post<SavedPrompt>('/v1/chat/prompts',
        { title, text, session_id: sessionId }),
    );
  }

  editSavedPrompt(id: string, title: string, text: string): Promise<SavedPrompt> {
    return firstValueFrom(
      this.http.patch<SavedPrompt>(`/v1/chat/prompts/${id}`, { title, text }),
    );
  }

  deleteSavedPrompt(id: string): Promise<{ deleted: boolean }> {
    return firstValueFrom(
      this.http.delete<{ deleted: boolean }>(`/v1/chat/prompts/${id}`),
    );
  }

  /** One consolidated prompt built from the selected one and its thread.
   *  Always resolves: the server returns the original on any failure. */
  sharpenPrompt(
    text: string,
    turns: { role: string; text: string }[] = [],
  ): Promise<{ title: string; text: string }> {
    return firstValueFrom(
      this.http.post<{ title: string; text: string }>(
        '/v1/chat/prompts/sharpen', { text, turns }),
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
  /** Change one element of a design to order, leaving the rest alone. */
  editElement(
    id: string,
    body: {
      html: string;
      instruction: string;
      label?: string;
      path?: string;
      model?: string;
    },
  ): Promise<{ html: string; saw?: boolean }> {
    return firstValueFrom(
      this.http.post<{ html: string; saw?: boolean }>(
        `/v1/design/projects/${id}/element`,
        body,
      ),
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
    signal?: AbortSignal,
  ): Promise<DesignProject> {
    // fetch rather than HttpClient: a run this long needs to be abortable, and
    // Cancel has to actually stop the request, not just look away from it.
    return fetch(`/v1/design/projects/${id}/generate`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ prompt, model, images }),
      signal,
    }).then(async (res) => {
      if (!res.ok) throw new Error((await res.text()) || res.statusText);
      return (await res.json()) as DesignProject;
    });
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

  /** Answer a question the model asked. The waiting turn resumes on the
   *  server; nothing comes back but an acknowledgement. */
  answerQuestion(
    sessionId: string,
    requestId: string,
    body: { chosen: string[]; other: string; skipped: boolean },
  ): Promise<unknown> {
    return firstValueFrom(
      this.http.post(`/v1/sessions/${sessionId}/questions/${requestId}`, body),
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

  /** Ask the builder to change the pipeline. Streams the same events the
   *  Code console renders, because it is the same loop with a different tool
   *  set — every edit is already written to the store as it happens, so the
   *  canvas only needs refreshing when the turn ends. */
  async streamBuild(
    pipelineId: string,
    content: string,
    onEvent: (event: CompassEvent) => void,
    effort = 'medium',
  ): Promise<void> {
    return this.streamPost(
      `/v1/pipelines/${pipelineId}/build`,
      { content, effort },
      onEvent,
    );
  }

  /** The builder conversation for a pipeline, so opening one shows how it
   *  came to look the way it does rather than an empty panel. */
  buildHistory(pipelineId: string): Promise<{
    session_id: string;
    messages: { role: 'user' | 'assistant'; text: string }[];
  }> {
    return firstValueFrom(
      this.http.get<{
        session_id: string;
        messages: { role: 'user' | 'assistant'; text: string }[];
      }>(`/v1/pipelines/${pipelineId}/build`),
    );
  }

  resetBuild(pipelineId: string): Promise<{ cleared: boolean }> {
    return firstValueFrom(
      this.http.delete<{ cleared: boolean }>(`/v1/pipelines/${pipelineId}/build`),
    );
  }

  /** Re-run the last user turn, discarding the previous answer. */
  async streamRegenerate(
    sessionId: string,
    onEvent: (event: CompassEvent) => void,
  ): Promise<void> {
    return this.streamPost(`/v1/sessions/${sessionId}/regenerate`, {}, onEvent);
  }

  // -- Missions: long-running builds ---------------------------------------
  async missions(): Promise<MissionSummary[]> {
    const res = await firstValueFrom(
      this.http.get<{ missions: MissionSummary[] }>('/v1/missions'));
    return res.missions ?? [];
  }

  async mission(id: string): Promise<MissionDetail> {
    return firstValueFrom(this.http.get<MissionDetail>(`/v1/missions/${id}`));
  }

  async createMission(goal: string, budgetUsd: number,
                      workspace = ''): Promise<MissionSummary> {
    return firstValueFrom(this.http.post<MissionSummary>('/v1/missions', {
      goal, budget_usd: budgetUsd, workspace,
    }));
  }

  /** Run sessions until the supervisor stops, streaming the agent's events
   *  and the supervisor's notes. The same reader the console uses, so the
   *  idle watchdog and the server's heartbeat apply here too — which matters
   *  more here than anywhere: a mission is hours, and a long silence has to
   *  be distinguishable from a dead connection. */
  async streamMission(id: string,
                      onEvent: (event: CompassEvent) => void,
                      force = false): Promise<void> {
    const q = force ? '?force=true' : '';
    return this.streamPost(`/v1/missions/${id}/run${q}`, {}, onEvent);
  }

  async abortMission(id: string): Promise<void> {
    await firstValueFrom(this.http.post(`/v1/missions/${id}/abort`, {}));
  }

  async deleteMission(id: string): Promise<void> {
    await firstValueFrom(this.http.delete(`/v1/missions/${id}`));
  }

  /** Watch a mission that is already running, without starting anything.
   *
   *  A GET rather than the POST above, and that distinction matters: reopening
   *  the page must never be what starts a build. `/run` starts-and-watches;
   *  this only watches, and answers 404 when there is nothing to watch. */
  async attachMission(id: string,
                      onEvent: (event: CompassEvent) => void): Promise<void> {
    return this.streamRequest(`/v1/missions/${id}/stream`, 'GET', null, onEvent);
  }

  private async streamPost(
    url: string,
    body: unknown,
    onEvent: (event: CompassEvent) => void,
  ): Promise<void> {
    return this.streamRequest(url, 'POST', body, onEvent);
  }

  private async streamRequest(
    url: string,
    method: 'GET' | 'POST',
    body: unknown,
    onEvent: (event: CompassEvent) => void,
  ): Promise<void> {
    // Raw fetch (EventSource can't POST or carry these headers, HttpClient
    // buffers) — the auth cookie rides along via credentials; the interceptor
    // can't see this call.
    const controller = new AbortController();
    const res = await fetch(url, {
      method,
      headers: method === 'POST' ? { 'content-type': 'application/json' } : {},
      credentials: 'include',
      body: method === 'POST' ? JSON.stringify(body ?? {}) : undefined,
      signal: controller.signal,
    });
    if (res.status === 401) {
      this.auth.sessionExpired();
      throw new Error('authentication required');
    }
    if (!res.ok || !res.body) {
      // Carry the status. Without it a caller can only say something generic,
      // and "could not reach the service" is a poor description of a 404 from
      // a service that answered.
      throw new Error(`${res.status} ${(await res.text()) || res.statusText}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await this.readOrGiveUp(reader, controller);
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

  /** How long a stream may say nothing at all before we treat it as dead.
   *
   *  A turn is allowed to be silent — a long shell command says nothing for
   *  minutes — so a timer on its own would kill honest work. The server pings
   *  every 15s for as long as a turn is alive (compass/common/sse.py), which
   *  is what makes silence meaningful: this is five missed pings, not a limit
   *  on how long a turn may take.
   *
   *  Without it, a request the gateway drops mid-flight leaves the browser
   *  waiting on a socket nobody will write to again — a spinner that ran for
   *  half an hour with a stop button that looked like progress. */
  private static readonly STREAM_IDLE_MS = 75_000;

  /** `reader.read()`, but it eventually gives up.
   *
   *  Rejecting is the point: the callers all reset their streaming state in a
   *  `catch`/`finally`, so an abandoned turn stops looking like a live one. */
  private readOrGiveUp(
    reader: ReadableStreamDefaultReader<Uint8Array>,
    controller: AbortController,
  ): Promise<ReadableStreamReadResult<Uint8Array>> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        controller.abort();
        reject(new Error(
          'the connection went quiet and the turn was lost — nothing further ' +
          'is coming. The work up to here is saved; send the message again to ' +
          'carry on.',
        ));
      }, CompassApiService.STREAM_IDLE_MS);
      const stop = () => clearTimeout(timer);
      reader.read().then(
        (result) => { stop(); resolve(result); },
        (err) => { stop(); reject(err); },
      );
    });
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
