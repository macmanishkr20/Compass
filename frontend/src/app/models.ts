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
  /** Whether the Pipelines module is mounted. The nav reads this rather than
   *  assuming: with the flag off there are no pipeline routes, so an entry
   *  leading to them would be a link to nothing. */
  pipelines?: boolean;
  /** Whether the Estimate module is mounted. Same contract as `pipelines`:
   *  the nav reads it rather than assuming, so a build with the flag off
   *  shows no entry leading to routes that are not there. */
  estimate?: boolean;
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
  /** The model's reasoning for this turn, when it reasoned. Shown above the
   *  answer and kept apart from it: this is the working, not the reply. A
   *  turn answered outright has none, which is normal for a reasoning model. */
  thinking?: string;
  /** What that reasoning cost. Billed as output, sharing the cap with the
   *  answer — the number that explains a turn that ran out of room. */
  thinkingTokens?: number;
  /** Still streaming. */
  thinkingLive?: boolean;
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
  /** The call's arguments as the model writes them, before it has finished.
   *  Unvalidated and often cut off mid-string — shown so a large parameter is
   *  visible while it is produced, never parsed or acted on. Replaced by the
   *  real `args` the moment the finished call arrives. */
  argsDraft?: string;
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

/** A question the model put to the person, and how it was settled. */
export interface QuestionVM {
  kind: 'question';
  id: string; // request_id
  question: string;
  header: string;
  options: Array<{ label: string; description: string }>;
  multiSelect: boolean;
  agentId?: string | null;
  /** Labels currently ticked. One at a time unless multiSelect. */
  picked: string[];
  /** What they are writing instead of picking. */
  other: string;
  /** Set once answered, so the card settles instead of staying live. */
  answered?: { chosen: string[]; other: string; skipped: boolean };
}

export interface NoticeVM {
  kind: 'notice';
  id: string;
  /** 'warn' is a turn the model declined — not a failure, so not an
   *  error, but more than information. */
  tone: 'info' | 'compaction' | 'error' | 'complete' | 'warn';
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

/** One node type as the server describes it.
 *
 *  `config_schema` is JSON Schema and is what the settings pane renders from,
 *  which is why the frontend never needs to know what a node actually does —
 *  a type added by a provider arrives fully described. */
export interface NodeTypeInfo {
  id: string;
  label: string;
  category: string;
  description: string;
  icon: string;
  config_schema: Record<string, unknown>;
  connection_kind: string;
  inputs: { name: string; kind: string; label: string }[];
  outputs: { name: string; kind: string; label: string }[];
  /** A capability the pipeline must hold before this node may run. */
  requires: string;
  /** What this step produces when nothing may be called. Present only on
   *  types that declare one. */
  sample?: Record<string, unknown> | null;
}

/** One node, mirroring compass/pipelines/store.py. The common fields are the
 *  ones every node needs whatever it does — a timeout, a retry policy, a way
 *  to keep a payload out of the log, and a way to switch it off. */
export interface PipelineNode {
  id: string;
  type: string;
  name: string;
  description: string;
  config: Record<string, unknown>;
  connection_id: string;
  position: { x: number; y: number };
  timeout_s: number;
  retries: number;
  retry_interval_s: number;
  secure_input: boolean;
  secure_output: boolean;
  /** Pinned output. Returned instead of calling anything in a mocked run and
   *  in any run started by hand; a scheduled run ignores it and calls for
   *  real, so a pipeline cannot quietly serve the same saved result forever. */
  mock: Record<string, unknown> | null;
  state: string;
  mark_as: string;
}

/** An edge carries the outcome it follows, so failure handling is what an
 *  arrow already is rather than a separate mechanism. */
export interface PipelineEdge {
  source: string;
  target: string;
  when: 'success' | 'failure' | 'completion' | 'skip';
  port: string;
}

export interface PipelineSummary {
  id: string;
  name: string;
  nodes: PipelineNode[];
  edges: PipelineEdge[];
  parameters: Record<string, unknown>;
  variables: Record<string, unknown>;
  capabilities: string[];
  /** How this pipeline starts on its own. The API has always sent these; the
   *  interface simply never said so. */
  triggers: Record<string, unknown>[];
  enabled: boolean;
  version: number;
  /** Set once a manual run has succeeded; scheduling is refused until then. */
  proven_at: number | null;
  /** An estimate this pipeline was costed against, or "". An opaque id: the
   *  server does not resolve it, because Pipelines and Estimate switch
   *  independently and a link that cannot resolve must not stop a pipeline
   *  loading. */
  estimate_id?: string;
  updated_at: number;
}

export interface PipelineNodeRun {
  node_id: string;
  status: string;
  /** The settings the node actually ran with, after expressions resolved.
   *  Empty when the node asked to keep its input out of the log. */
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  text: string;
  port: string;
  error: string;
  attempts: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface PipelineRun {
  id: string;
  pipeline_id: string;
  pipeline_name: string;
  pipeline_version: number;
  trigger: string;
  /** "live" called the world; "mock" touched nothing outside. Shown in the
   *  log, because a green mock run and a green live run mean very different
   *  things. */
  mode: string;
  status: string;
  nodes: Record<string, PipelineNodeRun>;
  /** Loop bodies: the loop's node id, then one entry per item. A body node
   *  has one state per item, so it cannot live in `nodes` — the canvas
   *  summarises across these, and the run panel opens a single item. */
  iterations: Record<string, Record<string, PipelineNodeRun>[]>;
  waiting_on: string;
  started_at: number;
  finished_at: number | null;
}

/** What `GET /v1/pipelines/{id}/export` returns: the pipeline as something
 *  that can leave — a diagram, an architecture note, and a runnable package. */
export interface PipelineExport {
  name: string;
  package: string;
  mermaid: string;
  architecture: string;
  files: { path: string; content: string }[];
  /** Node types that could not be exported as working code. Each is a named
   *  stub in the package that raises until the host registers one. */
  needs_host: { type: string; label: string; requires: string; connection: string }[];
}

/** A connection kind a built-in connector expects, with the caveat that
 *  applies to it — some auth is a token that lasts, some expires hourly. */
export interface ConnectionKind {
  kind: string;
  label: string;
  auth: string;
  note: string;
}

export interface PipelineConnection {
  id: string;
  kind: string;
  name: string;
  auth: string;
  config: Record<string, unknown>;
  has_secret: boolean;
  /** Every required field is filled in. Not the same as working — that is
   *  what the test answers. */
  configured?: boolean;
  signed_in?: boolean;
  needs_sign_in?: boolean;
  expires_at?: number | null;
  expired?: boolean;
  auth_kind?: string;
}

/** One field on a credential form. Mirrors n8n's credential `properties`:
 *  the browser renders the form from what the server declares, so the two
 *  cannot drift apart. */
export interface CredentialField {
  name: string;
  label: string;
  kind: string;
  required: boolean;
  default: string;
  help: string;
  from_oauth: boolean;
}

export interface CredentialTypeInfo {
  kind: string;
  label: string;
  /** "token" | "oauth2" | "none" */
  auth: string;
  fields: CredentialField[];
  test_url: string;
  scopes: string[];
  setup_note: string;
  docs_url: string;
  /** Registered with the provider ahead of time, so it is shown to be copied
   *  into their console — the most common thing to get wrong. */
  redirect_uri: string;
  has_client_default: boolean;
}

export interface PipelineProblem {
  node: string;
  problem: string;
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
  | PreviewCardVM
  | QuestionVM;

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
  type: string;          // color | select | range | toggle | text
  var: string;
  value: string;
  options?: string[];
  /** A range's bounds, and the unit its value carries. */
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
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

/** One question in the form Compass asks when a brief is too thin. */
export interface DesignClarifyField {
  id: string;
  label: string;
  hint?: string;
  type: 'text' | 'textarea' | 'segmented' | 'radio' | 'checkbox' | string;
  options?: string[];
  placeholder?: string;
  value?: string;
  max?: number;
}

export interface DesignClarify {
  ready: boolean;
  title?: string;
  subtitle?: string;
  waiting?: string;   // the project's name while it waits
  note?: string;      // what the conversation says it is waiting on
  fields?: DesignClarifyField[];
}

/** One page of a design project. */
export interface DesignPage {
  id: string;
  name: string;
  updated_at: number;
  chars: number;
}

/** A file or folder inside a project's own folder. */
export interface DesignFile {
  name: string;
  path: string;
  kind: string;
  size: number;
  at: number;
  text: boolean;
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
  template?: string;   // the template chip shown under a user turn
  files?: string[];    // attachments sent with it, shown as chips on the bubble
  answers?: Array<{ label: string; value: string }>;   // a filled-in form, shown as a card
  steps?: string[];   // the work the turn did, shown as collapsible rows
  file?: string;      // the document it wrote, shown as a chip
  /** Written without deliberation or web research, because the reasoning call
   *  failed. Flagged rather than left to prose: the document itself looks
   *  entirely normal, and the reader has no other way to know that anything
   *  recent in it may be years out of date. */
  degraded?: boolean;
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
  /** The question form the project is waiting on, if it is waiting on one. */
  clarify?: DesignClarify | null;
  awaiting?: boolean;     // on a list row: it is waiting on a form
  empty?: boolean;        // on a list row: nothing rendered yet, so no thumbnail
  versions?: number;      // on a list row: how many past versions exist
  version_label?: string;
  viewed_at?: number;
  created_at: number;
  updated_at: number;
}
