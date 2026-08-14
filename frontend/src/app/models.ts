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

/** One entry in the Files tree. */
export interface FileEntry {
  name: string;
  path: string;
  dir: boolean;
  size: number;
}

/** A filename or content-search hit in the Files panel. */
export interface FileHit {
  path: string;
  line: number;
  text: string;
}

/** Settings → Customize: what Compass can do and what it is connected to. */
export interface CustomizeInfo {
  tools: { name: string; description: string }[];
  connectors: { name: string; detail: string; connected: boolean }[];
  mcp_servers: { name: string; detail: string; connected: boolean }[];
  mcp_tools: string[];
  routines: { name: string; detail: string }[];
}

/** "How you've been working with Compass" — the Settings → Reflect recap. */
export interface Recap {
  days: number;
  conversations: number;
  agent_conversations: number;
  chat_conversations: number;
  top_day: { day: string; count: number } | null;
  peak_hour: { hour: number; label: string; count: number } | null;
  by_day: { day: string; count: number }[];
  topics: { topic: string; count: number }[];
  observations: string[];
}

/** One thing Compass remembers about the user, shown in Settings → Memory
 *  grouped by category (Claude's memory model: individual categorized entries
 *  the model reads and updates while you chat). */
export interface MemoryEntry {
  id: string;
  scope: string;
  category: string;
  summary: string;
  details: string;
  created_at: number;
  updated_at: number;
}

/** A browser-preview card the agent produced (the `browser` tool) — the app
 *  screenshot plus a header with the page title, its URL, and an Open button
 *  that opens the live page in the Compass browser pane (like claude.ai). */
export interface PreviewCardVM {
  kind: 'preview';
  id: string;
  imageUrl: string;
  pageUrl: string;
  title: string;
}

export type TimelineItem =
  | ChatBubble
  | ToolCardVM
  | PermissionVM
  | NoticeVM
  | PreviewCardVM;

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

/** A Design template card on the Design landing screen. */
export interface DesignTemplate {
  id: string;
  name: string;
  hint: string;
  /** The opening words the composer is seeded with when this is picked. */
  stem?: string;
}

/** One knob a design declares as tweakable. */
export interface DesignTweak {
  name: string;
  type: 'color' | 'select' | string;
  var: string;
  value: string;
  options?: string[];
}

/** One piece of design work. `html` is only present on a single-project fetch —
 *  the list endpoint omits it because a design can be tens of kilobytes. */
/** A house style designs can be told to follow. */
export interface DesignSystem {
  id: string;
  name: string;
  source: string;
  notes: string;
  css: string;
  fonts?: string;
  swatches?: string[];
  origin?: string;
  builtin?: boolean;
  font_display?: string;
  font_body?: string;
  created_at: number;
  updated_at: number;
}

/** One page of a design system's project. */
export interface DesignSection {
  id: string;
  group: string;
  name: string;
  file: string;
  blurb: string;
}

/** A design system opened as a browsable project. */
export interface DesignSystemDoc {
  name: string;
  notes: string;
  sections: DesignSection[];
  params: Record<string, string>;
  swatches: string[];
  usage: Record<string, string>;
}

/** One past state of a design. `html` only comes back on a restore. */
export interface DesignVersion {
  id: string;
  at: number;
  label: string;
}

/** A pin left on the canvas, positioned as a fraction of the design's box so
 *  it stays put when the preview is scaled. */
export interface DesignComment {
  id: string;
  x: number;
  y: number;
  text: string;
  author: string;
  resolved: boolean;
  at: number;
}

export interface DesignTurn {
  role: 'user' | 'assistant';
  text: string;
  steps?: string[];   // the work the turn did, shown as collapsible rows
  file?: string;      // the document it wrote, shown as a chip
  vote?: 'up' | 'down';
}

export interface DesignProject {
  id: string;
  name: string;
  template: string;
  prompt: string;
  html?: string;
  turns?: DesignTurn[];
  comments?: DesignComment[];
  design_system: string;
  design_systems?: string[];
  starred: boolean;
  versions?: number;      // on a list row: how many past versions exist
  version_label?: string;
  viewed_at?: number;
  created_at: number;
  updated_at: number;
}
