// Wire types mirroring compass/models/events.py and the REST surface.

export interface HealthInfo {
  status: string;
  mock_model: boolean;
  deployment: string;
  models: string[];
  github: boolean;
  storage_backend: string;
  telemetry: boolean;
  auth: boolean;
  tts: boolean;
  tts_voice: string;
  tts_voices: string[];
  mcp_servers: Record<string, string>;
  mcp_tools: string[];
  workspace: string;
}

export interface GitStatus {
  branch: string;
  remote: string;
  is_git: boolean;
  added: number;
  removed: number;
  files_changed: number;
  untracked: number;
  ahead: number;
  dirty: boolean;
}

export interface Workspace {
  id: string;
  name: string;
  path: string;
  kind: string; // "local" | "github"
  remote_url: string;
  branch: string;
  exists: boolean;
  is_git: boolean;
}

export interface GithubRepo {
  full_name: string;
  default_branch: string;
  private: boolean;
  description: string;
  updated_at: string;
  html_url: string;
}

export type PermissionBehavior = 'allow' | 'deny' | 'timeout' | 'allow_always';

export interface CompassEvent {
  type: string;
  agent_id?: string | null;
  [key: string]: unknown;
}

/** A raw uploaded file sent to the backend, which classifies + extracts it
 *  (images → gpt-5 vision, PDF/DOCX/ZIP/text → inlined text). Used by both the
 *  Home/Chat and Agent Console composers. */
export interface ChatAttachment {
  name: string;
  mime: string;
  data_url: string;
}

/** A Home/Chat conversation in the sidebar list. */
export interface ChatCard {
  id: string;
  title: string;
  pinned?: boolean;
  updated_at: number;
  created_at: number;
}

// UI-side view models -------------------------------------------------------

export type Role = 'user' | 'assistant';

export interface ChatBubble {
  kind: 'bubble';
  id: string;
  role: Role;
  text: string;
  agentId?: string | null;
  streaming?: boolean;
  msgUuid?: string; // server message uuid (backfilled) — enables edit
  editing?: boolean;
  at?: number; // epoch ms — shown on hover (user prompts)
  stats?: { ms: number; tokens: number }; // per-response, set at stream end
  atts?: UiAttachmentVM[]; // files/images attached to a user prompt
}

/** Attachment shown on a user bubble (mirror of attachments.ts UiAttachment). */
export interface UiAttachmentVM {
  id: string;
  name: string;
  mime: string;
  kind: 'image' | 'file';
  size: number;
  dataUrl: string;
}

export type ToolStatus = 'running' | 'ok' | 'error';

export interface ToolCardVM {
  kind: 'tool';
  id: string; // tool_call_id
  name: string;
  args: string;
  output: string;
  status: ToolStatus;
  durationMs?: number;
  agentId?: string | null;
  isMcp: boolean;
}

export interface PermissionVM {
  kind: 'permission';
  id: string; // request_id
  toolCallId: string;
  toolName: string;
  args: string;
  reason: string;
  agentId?: string | null;
  resolved?: PermissionBehavior;
}

export interface NoticeVM {
  kind: 'notice';
  id: string;
  tone: 'info' | 'compaction' | 'error' | 'complete';
  text: string;
}

export type TimelineItem = ChatBubble | ToolCardVM | PermissionVM | NoticeVM;

export interface UsageVM {
  promptTokens: number;
  cachedPromptTokens: number;
  completionTokens: number;
  costUsd: number;
}

export interface SessionCard {
  id: string;
  title: string;
  pinned: boolean;
  archived: boolean;
  group: string;
  mode: string;
  effort: string;
  model: string;
  workspace: string;
  routine_id?: string;
  created_at: number;
  updated_at: number;
  message_count: number;
}

export type ArtifactKind = 'html' | 'svg' | 'mermaid' | 'drawio' | 'azure';

export interface BackgroundTask {
  id: string;
  name: string;
  command: string;
  status: 'running' | 'finished' | 'stopped' | 'error';
  started_at: number;
  finished_at: number | null;
  elapsed_ms: number;
  exit_code: number | null;
  url: string | null;
  workspace_id: string | null;
}

export interface BackgroundTasksResponse {
  tasks: BackgroundTask[];
  running: number;
  finished: number;
}

export type TriggerType = 'once' | 'hourly' | 'daily' | 'weekdays' | 'weekly' | 'custom';

export interface RoutineTrigger {
  type: TriggerType;
  time: string; // HH:MM 24h
  days: number[]; // weekly: 0=Mon..6=Sun
  cron: string;
  date: string;
}

export interface Routine {
  id: string;
  name: string;
  prompt: string;
  triggers: RoutineTrigger[];
  schedule: string; // computed human summary
  target: 'local' | 'cloud';
  model: string;
  repository: string;
  connectors: string[];
  behavior: { auto_fix_prs: boolean };
  notifications: { enabled: boolean; push: boolean; email: boolean; slack: boolean };
  enabled: boolean;
  created_at: number;
  updated_at: number;
  last_run_at: number | null;
  next_run_at: number | null;
  next_run_label: string | null;
}

export interface RoutineRun {
  id: string;
  routine_id: string;
  routine_name: string;
  trigger: 'scheduled' | 'manual' | 'api' | 'webhook';
  status: 'running' | 'completed' | 'failed';
  started_at: number;
  finished_at: number | null;
  session_id: string;
  summary: string;
}

export interface RoutineTemplate {
  id: string;
  icon: string;
  name: string;
  description: string;
  schedule: string;
  trigger_type: TriggerType;
  time: string;
  integrations: string[];
  prompt: string;
}

export interface RoutinesResponse {
  routines: Routine[];
  templates: RoutineTemplate[];
  suggestions: string[];
  connectors: string[];
}

export interface Artifact {
  id: string;
  title: string;
  kind: ArtifactKind;
  code: string;
}

export type GroupBy = 'none' | 'group' | 'date';
export type SortBy = 'recent' | 'created' | 'title';

export interface SessionGroup {
  label: string;
  cards: SessionCard[];
}
