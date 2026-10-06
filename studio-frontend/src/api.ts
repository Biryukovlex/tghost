export type SetupState = {
  enabled: boolean;
  ready: boolean;
  workspace_slug: string;
  provider: string;
  blockers: Array<{ code: string; message: string }>;
};

export type Channel = {
  id: number;
  identifier: string;
  title: string;
  active?: boolean;
};

export type Conversation = {
  id: string;
  workspace_id: string;
  channel_id: number;
  channel_identifier: string;
  channel_title: string;
  title: string;
  summary: string;
  active_draft_id?: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

export type DraftClaim = {
  claim: string;
  source_ids: string[];
  passage?: string;
  passages?: Record<string, string>;
  verified?: boolean;
};

export type Draft = {
  media_ids?: string[];
  id: string;
  workspace_id: string;
  conversation_id: string;
  channel_id: number;
  story_cluster_id?: string | null;
  analysis_id?: string | null;
  working_title: string;
  title?: string;
  source_links?: string[];
  body: string;
  body_html?: string;
  body_plain?: string;
  plain_character_count?: number;
  status: string;
  source_ids: string[];
  claim_support: DraftClaim[];
  assumptions: string[];
  warnings: string[];
  channel_evidence: Array<Record<string, unknown>>;
  web_evidence: Array<Record<string, unknown>>;
  confidence: "high" | "medium" | "low" | string;
  creative: boolean;
  provider: string;
  model: string;
  prompt_version: string;
  revision: number;
  current_version: number;
  current_version_origin: string;
  character_count: number;
  over_limit: boolean;
  warning_threshold: boolean;
  copied_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type DraftVersion = {
  media_ids?: string[];
  id: number;
  draft_id: string;
  version: number;
  body: string;
  working_title?: string | null;
  title?: string | null;
  source_links?: string[] | null;
  origin: "generated" | "regenerated" | "user_edit" | string;
  instruction: string;
  character_count: number;
  created_at: string | null;
};

export type PersistedMessage = {
  id: number;
  role: "user" | "assistant" | "system_summary";
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type RunSummary = {
  id: string;
  conversation_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";
  stage: string;
  provider: string;
  requested_model: string;
  actual_model: string | null;
  usage: RunUsage;
  error_code: string | null;
  error_message: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
};

export type RunUsage = {
  requests: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  estimated_cost_usd?: number;
  latency_ms?: number;
  details?: Record<string, number>;
};

export type RunActivity = {
  event_count: number;
  tool_call_count: number;
  result_count: number;
  source_count: number;
  story_count: number;
  cache_hit_count: number;
  degraded: boolean;
  search_outcome?: "healthy" | "partial" | "empty" | "unavailable" | string;
  failed_engines?: string[];
  references: Record<string, string[]>;
};

export function describeSearchOutcome(activity: Pick<RunActivity, "degraded" | "search_outcome" | "failed_engines">): string | null {
  const outcome = activity.search_outcome;
  const failed = (activity.failed_engines ?? []).filter((name) => name.trim() !== "");
  if (outcome === "partial") {
    return failed.length > 0
      ? `Research was partial for this run (${failed.join(", ")} failed); returned sources are still usable.`
      : "Research was partial for this run; returned sources are still usable.";
  }
  if (outcome === "unavailable") {
    return failed.length > 0
      ? `Research was unavailable for this run (${failed.join(", ")} failed).`
      : "Research was unavailable for this run.";
  }
  if (outcome === "empty") {
    return "Research completed with no results for this run.";
  }
  if (activity.degraded) {
    return "Research was degraded for this run.";
  }
  return null;
}

export type RunDetails = {
  provider: string;
  requested_model: string;
  actual_model: string | null;
  prompt_version: string;
  status: RunSummary["status"];
  stage: string;
  duration_ms: number | null;
  usage: RunUsage;
  activity: RunActivity;
  error: { code: string; message: string; retryable: boolean } | null;
};

export type RunEvent = {
  id: number;
  sequence: number;
  event_type: string;
  safe_payload: Record<string, unknown>;
  created_at: string | null;
};

export type ChannelProfile = {
  channel_id: number;
  version: number;
  topics_text: string;
  editorial_text: string;
  style_text: string;
  updated_at: string | null;
  built_at: string | null;
  built_from_posts: number;
  topics?: Array<{ name: string; claim?: string }>;
  confidence?: string;
  style_profile?: Record<string, unknown>;
  editorial_rules?: Record<string, unknown>;
  /** Supporting-post evidence (T37–T39 backend work exposes this when ready). */
  evidence_post_ids?: number[];
};

export type ProfilePayload = {
  profile: ChannelProfile | null;
  can_build: boolean;
  build_blockers: Array<{ code: string; message: string; available?: number; minimum?: number }>;
  channel_id: number;
};

export type ProfileDraft = {
  topics_text: string;
  editorial_text: string;
  style_text: string;
  built_from_posts: number;
  limitations: string[];
  formatting_facts: Array<Record<string, unknown>>;
  /** Supporting-post evidence (T37–T39 backend work exposes this when ready). */
  evidence_post_ids?: number[];
};

export type Bootstrap = {
  setup: SetupState;
  workspace: { id: string; slug: string; role: string };
  user: { id: string | null };
  provider: { name: string; model: string; configured: boolean };
  channels: Channel[];
  selected_channel_id: number | null;
  conversations: Conversation[];
  current_conversation: Conversation | null;
  draft: Draft | null;
  active_run: RunSummary | null;
  consent: {
    provider: string;
    granted: boolean;
    required: boolean;
    configuration_fingerprint?: string;
    disclosure?: { title: string; message: string } | null;
  };
  profile_status: "no_channel" | "needs_consent" | "not_built" | "not_analyzed" | "low_confidence" | "ready";
  profile: ChannelProfile | null;
};

export type ApiError = {
  error?: { code?: string; message?: string; retryable?: boolean };
  server_draft?: Draft;
  local_draft?: Record<string, unknown>;
  current_revision?: number;
};

export class StudioApiError extends Error {
  status: number;
  payload: ApiError;

  constructor(status: number, payload: ApiError) {
    super(payload.error?.message ?? "The Studio request failed.");
    this.name = "StudioApiError";
    this.status = status;
    this.payload = payload;
  }
}

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...init,
    headers: {
      accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const contentType = response.headers.get("content-type") ?? "";
  // Session expired: fetch follows the 303 to /login and returns HTML 200.
  // Detect redirect, 401, or non-JSON and force a login navigation.
  if (response.redirected || response.status === 401 || !contentType.includes("application/json")) {
    try {
      window.location.assign("/login");
    } catch {
      /* test environment without window */
    }
    throw new StudioApiError(401, {
      error: { code: "unauthenticated", message: "Sign in to continue.", retryable: false },
    });
  }
  let payload: T & ApiError;
  try {
    payload = (await response.json()) as T & ApiError;
  } catch {
    try {
      window.location.assign("/login");
    } catch {
      /* ignore */
    }
    throw new StudioApiError(401, {
      error: { code: "unauthenticated", message: "Sign in to continue.", retryable: false },
    });
  }
  if (!response.ok) {
    throw new StudioApiError(response.status, payload);
  }
  return payload as T;
}

export function csrfToken(): string {
  return document.querySelector<HTMLMetaElement>("meta[name=studio-csrf-token]")?.content ?? "";
}

export async function fetchProfile(channelId: number): Promise<ProfilePayload> {
  return api<ProfilePayload>(`/studio/api/profile?channel_id=${encodeURIComponent(String(channelId))}`);
}

export async function saveProfile(payload: { channel_id: number; expected_version: number; topics_text: string; editorial_text: string; style_text: string }): Promise<{ profile: ChannelProfile }> {
  return api<{ profile: ChannelProfile }>("/studio/api/profile", {
    method: "PUT",
    headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
    body: JSON.stringify(payload),
  });
}

export async function buildProfile(channelId: number): Promise<{ draft: ProfileDraft }> {
  return api<{ draft: ProfileDraft }>("/studio/api/profile/build", {
    method: "POST",
    headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
    body: JSON.stringify({ channel_id: channelId }),
  });
}

export function asThreadMessages(messages: PersistedMessage[]) {
  return messages
    .flatMap((message) => {
      if (message.role !== "user" && message.role !== "assistant") return [];
      return [{
        id: `persisted-${message.id}`,
        role: message.role,
        content: [{ type: "text" as const, text: message.content }],
      }];
    });
}

export type StudioPostMetrics = {
  views: number;
  reactions: number;
  comments: number;
  shares: number;
  collected_comments?: number;
  updated_at?: string | null;
};

export type StudioPost = {
  id: number;
  message_id: number;
  channel_id: number;
  channel_identifier: string;
  channel_title: string;
  posted_at: string;
  text: string;
  formatting_entities: Array<Record<string, unknown>>;
  formatted_html: string;
  metrics: StudioPostMetrics;
  source_url: string | null;
};

export async function fetchStudioPost(postId: number): Promise<StudioPost> {
  if (!Number.isInteger(postId) || postId <= 0) {
    throw new StudioApiError(404, { error: { code: "not_found", message: "Post not found.", retryable: false } });
  }
  return api<StudioPost>(`/api/posts/${encodeURIComponent(String(postId))}`);
}

/**
 * T44 prefill contract (consumed by T52's Post Explorer handoff).
 *
 * The reader passes an authorized post/channel reference — never raw channel
 * content in the URL — and Studio prefills the composer without sending:
 *
 * - Same-tab: `window.dispatchEvent(new CustomEvent("tg-studio:prefill",
 *   { detail: { channel_id, text, post_id? } }))`.
 * - Cross-page (`/post/{id}` -> `/studio`): T52 writes
 *   `sessionStorage["tg-studio:prefill"] = JSON.stringify({ channel_id, text,
 *   post_id? })`; Studio consumes (and removes) it once on load.
 *
 * Studio switches to the post's own channel under the usual unsaved-edit
 * guard, never sends a message, and never moves another channel's chats,
 * prompts, memory or drafts into the active conversation.
 */
export const PREFILL_EVENT = "tg-studio:prefill";
export const PREFILL_STORAGE_KEY = "tg-studio:prefill";
export const PREFILL_MAX_TEXT = 4000;

export type StudioPrefill = {
  channel_id: number;
  text: string;
  post_id?: number | string;
};

export function parsePrefillDetail(detail: unknown): StudioPrefill | null {
  if (!detail || typeof detail !== "object") return null;
  const record = detail as Record<string, unknown>;
  const channelId = Number(record.channel_id);
  const text = typeof record.text === "string" ? record.text : "";
  if (!Number.isInteger(channelId) || channelId <= 0) return null;
  const trimmed = text.trim();
  if (!trimmed) return null;
  const prefill: StudioPrefill = {
    channel_id: channelId,
    text: trimmed.slice(0, PREFILL_MAX_TEXT),
  };
  const postId = record.post_id;
  if ((typeof postId === "number" && Number.isInteger(postId) && postId > 0) || typeof postId === "string") {
    prefill.post_id = postId;
  }
  return prefill;
}

export function consumeStoredPrefill(storage?: Storage | undefined): StudioPrefill | null {
  let store: Storage | undefined = storage;
  if (store === undefined) {
    try {
      store = window.sessionStorage;
    } catch {
      return null;
    }
  }
  if (!store) return null;
  let raw: string | null;
  try {
    raw = store.getItem(PREFILL_STORAGE_KEY);
    store.removeItem(PREFILL_STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    return parsePrefillDetail(JSON.parse(raw) as unknown);
  } catch {
    return null;
  }
}
