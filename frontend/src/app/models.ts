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

export type PermissionBehavior = 'allow' | 'deny' | 'timeout';

export interface CompassEvent {
  type: string;
  agent_id?: string | null;
  [key: string]: unknown;
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
  created_at: number;
  updated_at: number;
  message_count: number;
}

export interface Artifact {
  id: string;
  title: string;
  kind: 'html' | 'svg';
  code: string;
}

export type GroupBy = 'none' | 'group' | 'date';
export type SortBy = 'recent' | 'created' | 'title';

export interface SessionGroup {
  label: string;
  cards: SessionCard[];
}
