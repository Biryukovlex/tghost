import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type ToolCallMessagePartProps,
  unstable_useComposerInput,
  useAuiState,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";
import { HttpAgent } from "@ag-ui/client";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import remarkGfm from "remark-gfm";
import {
  api,
  asThreadMessages,
  consumeStoredPrefill,
  csrfToken,
  PREFILL_EVENT,
  parsePrefillDetail,
  type ApiError,
  type Bootstrap,
  type Channel,
  type Conversation,
  type Draft,
  type DraftVersion,
  type PersistedMessage,
  type RunDetails,
  type RunEvent,
  type RunSummary,
  type RunUsage,
  type StudioPrefill,
  StudioApiError,
  describeSearchOutcome,
} from "./api";
import { describeRunFailure } from "./runFailure";
import { useDialogFocusTrap } from "./dialogFocus";
import { useMessageExpansion } from "./messageExpansion";
import { isTerminalPollStatus, nextPollDelay, shouldStopPollingAfterErrors } from "./runPolling";
import { copyRenderedSelection, copyRichText, htmlFromMarkdown, isBlankDraftBody, plainFromMarkdown, telegramMarkupFromMarkdown } from "./markdownCopy";
import ChannelProfileDialog from "./ChannelProfileDialog";
import "./styles.css";
import { ImageAttachments, ScheduleDialog } from "./publishing";
import { CalendarPage } from "./CalendarPage";

function ToolActivity({ toolName, result }: ToolCallMessagePartProps) {
  const label = toolActivityLabel(toolName);
  return (
    <div className="studio-tool" role="status">
      <span className="studio-tool-dot" aria-hidden="true" />
      {result === undefined ? `${label}…` : completedToolActivityLabel(toolName)}
    </div>
  );
}

function StudioMarkdownText() {
  return (
    <MarkdownTextPrimitive
      className="studio-markdown"
      defer
      remarkPlugins={[remarkGfm]}
      components={{
        img: () => null,
        table: ({ node: _node, ...props }) => <div className="studio-table-scroll" role="region" aria-label="Message table, scroll horizontally" tabIndex={0}><table {...props} /></div>,
        a: ({ node: _node, ...props }) => (
          <a {...props} target="_blank" rel="noopener noreferrer" />
        ),
      }}
    />
  );
}

export function MessageReader({ children, onClose }: { children: ReactNode; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useDialogFocusTrap(dialogRef);
  useEffect(() => {
    const opener = document.activeElement;
    dialogRef.current?.showModal();
    return () => { if (opener instanceof HTMLElement && opener.isConnected) opener.focus(); };
  }, []);
  return <dialog ref={dialogRef} className="studio-message-reader" aria-labelledby="message-reader-title" onCancel={onClose} onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <header><div><h2 id="message-reader-title">Agent response</h2><p>Read the full message. Scroll across wide tables.</p></div><button type="button" onClick={onClose} aria-label="Close message"><i className="mgc mgc-close-core-regular" aria-hidden="true" /></button></header>
    <div className="studio-message-reader-body" tabIndex={0} role="region" aria-label="Full message, scroll vertically and horizontally">{children}</div>
  </dialog>;
}

function StudioAssistantContent() {
  return <MessagePrimitive.Parts components={{ Text: StudioMarkdownText, tools: { Fallback: ToolActivity } }} />;
}

export function StudioMessage() {
  const [readerOpen, setReaderOpen] = useState(false);
  const role = useAuiState((state) => state.message.role);
  const hasContent = useAuiState((state) => state.message.content.some((part) =>
    part.type === "text" ? Boolean(part.text.trim()) : part.type === "tool-call",
  ));
  const { contentRef, canExpand } = useMessageExpansion(role === "assistant" && hasContent);
  // The run's Thinking indicator owns the pending state. Mount a response
  // bubble only when there is text or tool activity to show.
  if (role === "assistant" && !hasContent) return null;

  return (
    <MessagePrimitive.Root className="studio-message" data-role={role}>
      {canExpand && <button type="button" className="studio-message-expand" onClick={() => setReaderOpen(true)} aria-label="Open message in wide view" title="Expand message"><i className="mgc mgc-fullscreen-2-core-regular" aria-hidden="true" /></button>}
      <MessagePrimitive.If assistant>
        <div ref={contentRef}><StudioAssistantContent /></div>
      </MessagePrimitive.If>
      {readerOpen && <MessageReader onClose={() => setReaderOpen(false)}><StudioAssistantContent /></MessageReader>}
      <MessagePrimitive.If user>
        <MessagePrimitive.Parts components={{ tools: { Fallback: ToolActivity } }} />
      </MessagePrimitive.If>
      <MessagePrimitive.If system>
        <MessagePrimitive.Parts components={{ tools: { Fallback: ToolActivity } }} />
      </MessagePrimitive.If>
    </MessagePrimitive.Root>
  );
}

const TOOL_ACTIVITY_LABELS: Record<string, string> = {
  get_channel_context: "Reading channel history",
  analyze_channel: "Analyzing post performance",
  get_topic_profile: "Reviewing channel topics",
  search_web: "Searching the web",
  read_sources: "Reading source pages",
  compare_sources: "Cross-checking sources",
  find_novel_topics: "Finding fresh story angles",
  propose_topic_changes: "Preparing a topic proposal",
  apply_confirmed_topic_changes: "Updating the channel profile",
  explain_recommendation: "Connecting the evidence",
  get_draft: "Reading the draft",
  list_draft_versions: "Reading draft history",
  create_draft: "Building the draft artifact",
  edit_draft: "Revising the draft artifact",
};

const TOOL_ACTIVITY_RESULT_LABELS: Record<string, string> = {
  get_channel_context: "Channel context received",
  analyze_channel: "Channel analysis received",
  get_topic_profile: "Channel profile received",
  search_web: "Search results received",
  read_sources: "Source pages received",
  compare_sources: "Source comparison received",
  find_novel_topics: "Topic candidates received",
  propose_topic_changes: "Topic proposal received",
  apply_confirmed_topic_changes: "Channel profile updated",
  explain_recommendation: "Evidence received",
  get_draft: "Draft loaded",
  list_draft_versions: "Draft history loaded",
  create_draft: "Draft artifact saved",
  edit_draft: "Draft revision saved",
};

function toolActivityLabel(toolName: unknown): string {
  const normalized = typeof toolName === "string" ? toolName : "";
  if (normalized in TOOL_ACTIVITY_LABELS) return TOOL_ACTIVITY_LABELS[normalized];
  if (!normalized) return "Working with channel context";
  return `Working on ${normalized.replaceAll("_", " ")}`;
}

function completedToolActivityLabel(toolName: unknown): string {
  const normalized = typeof toolName === "string" ? toolName : "";
  return TOOL_ACTIVITY_RESULT_LABELS[normalized] ?? "Tool result received";
}

function buildRunHint(conversationId: string, id: string, status: RunSummary["status"]): RunSummary {
  return {
    id,
    conversation_id: conversationId,
    status,
    stage: status,
    provider: "openrouter",
    requested_model: "",
    actual_model: null,
    usage: { requests: 0, tool_calls: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    error_code: null,
    error_message: null,
    created_at: new Date().toISOString(),
    started_at: status === "running" ? new Date().toISOString() : null,
    finished_at: null,
    duration_ms: null,
  };
}

export function isConversationActiveRunError(error: unknown): boolean {
  return error instanceof StudioApiError
    && error.status === 409
    && error.payload.error?.code === "conversation_active_run";
}

export function isActiveRun(run: RunSummary | null | undefined): boolean {
  return run?.status === "queued" || run?.status === "running";
}

export type StudioScope = {
  channelId: number | null;
  conversationId: string | null;
  sequence: number;
};
/**
 * Channel/conversation isolation guard for async reads and streamed events.
 * A response that was requested under an older scope must never overwrite
 * the newly selected channel/conversation: delayed results from A stay out
 * of B. Sequence also covers same-scope refetches racing each other.
 */
export function isStaleScope(request: StudioScope, current: StudioScope): boolean {
  if (request.sequence !== current.sequence) return true;
  if (request.channelId !== current.channelId) return true;
  if (request.conversationId !== current.conversationId) return true;
  return false;
}

/**
 * Reconnection-safe event merge. Poll windows can overlap after a dropped
 * connection or a slow response, so incoming events are deduplicated by
 * sequence and re-sorted; only the newest `cap` are kept for rendering.
 * Terminal delivery is separate: the run status in the payload settles
 * pending activity, never this list.
 */
export function mergeRunEvents(current: RunEvent[], incoming: RunEvent[], cap = 24): RunEvent[] {
  const seen = new Map<number, RunEvent>();
  current.forEach((event) => {
    if (Number.isInteger(event.sequence)) seen.set(event.sequence, event);
  });
  incoming.forEach((event) => {
    if (Number.isInteger(event.sequence)) seen.set(event.sequence, event);
  });
  return [...seen.values()].sort((a, b) => a.sequence - b.sequence).slice(-Math.max(1, cap));
}

/** Setup blockers (consent, provider, readiness) get a Settings recovery path. */
export function isSetupBlockerCode(code: string | null | undefined): boolean {
  if (!code) return false;
  return /consent|not_ready|not_configured|setup|provider/i.test(code);
}

/** Move keyboard focus to the message composer so a retry starts there. */
export function focusComposer(): boolean {
  const input = document.querySelector(".studio-composer input, .studio-composer textarea");
  if (input instanceof HTMLElement) {
    input.focus();
    return true;
  }
  return false;
}

export type DraftPreview = { html: string; fromServer: boolean };

/**
 * Formatted Full post preview. Prefers the server-rendered sanitized
 * Telegram HTML (`body_html`, derived from stored text/entities) for a clean
 * saved draft; local edits and version views use the canonical Markdown
 * conversion shared with clipboard HTML. The small prototype renderer is
 * intentionally not shipped: commentary stays out of the publishable field.
 */
export function draftPreviewHtml(shownBody: string, draft: Draft | null, hasUnsavedEdits: boolean): DraftPreview {
  if (draft && !hasUnsavedEdits && draft.body_html && draft.body_html.trim()) {
    return { html: draft.body_html, fromServer: true };
  }
  return { html: htmlFromMarkdown(shownBody), fromServer: false };
}

async function studioAgentFetch(url: string, init: RequestInit): Promise<Response> {
  const response = await fetch(url, init);
  if (response.ok) return response;
  let payload: ApiError = {};
  try {
    payload = (await response.clone().json()) as ApiError;
  } catch {
    // Preserve the HTTP status when a proxy returns a non-JSON error page.
  }
  throw new StudioApiError(response.status, payload);
}

function describeAgentActivity(run: RunSummary, events: RunEvent[]): { label: string; detail: string; stage: string } {
  if (run.status === "queued") {
    return { label: "Waiting for the Studio agent", detail: "Your request is safely queued.", stage: "queued" };
  }

  const lastText = [...events].reverse().find((event) => event.event_type.startsWith("TEXT_MESSAGE_"));
  const lastToolStart = [...events].reverse().find((event) => event.event_type === "TOOL_CALL_START");
  const lastToolResult = [...events].reverse().find((event) => event.event_type === "TOOL_CALL_RESULT");
  const latestSequence = events.at(-1)?.sequence ?? 0;

  if (lastText && lastText.sequence === latestSequence) {
    return { label: "Writing the response", detail: "The answer will appear here as it is composed.", stage: "writing" };
  }
  if (lastToolStart && (!lastToolResult || lastToolStart.sequence > lastToolResult.sequence)) {
    const label = toolActivityLabel(lastToolStart.safe_payload.tool_name);
    return { label, detail: "Using only the scoped tools and context for this conversation.", stage: "tool" };
  }
  if (lastToolResult) {
    const toolName = lastToolResult.safe_payload.tool_name ?? lastToolStart?.safe_payload.tool_name;
    return {
      label: "Thinking with the latest results",
      detail: `${completedToolActivityLabel(toolName)}. The agent may continue working.`,
      stage: "thinking",
    };
  }
  return { label: "Thinking through your request", detail: "Choosing the next useful action from your channel context.", stage: "thinking" };
}

export function AgentActivity({ run, events, onStopRun }: { run: RunSummary | null; events: RunEvent[]; onStopRun: (runId: string) => Promise<void> }) {
  const active = isActiveRun(run);
  const [stopping, setStopping] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, run?.id]);

  useEffect(() => {
    if (!active) setStopping(false);
  }, [active, run?.id]);

  if (!run || !active) return null;
  const activity = describeAgentActivity(run, events);
  const startedAt = Date.parse(run.started_at || run.created_at || "");
  const elapsedSeconds = Number.isFinite(startedAt) ? Math.max(0, Math.floor((now - startedAt) / 1000)) : 0;

  return (
    <div className="studio-agent-activity" data-stage={activity.stage} role="status" aria-live="polite">
      <span className="studio-agent-orbit" aria-hidden="true"><span /></span>
      <span className="studio-agent-activity-copy">
        <strong>{activity.label}</strong>
        <small>{activity.detail}</small>
      </span>
      <span className="studio-agent-elapsed">{elapsedSeconds}s</span>
      <button
        type="button"
        className="studio-stop-run"
        disabled={stopping}
        onClick={() => {
          if (!run) return;
          setStopping(true);
          void onStopRun(run.id).catch(() => setStopping(false));
        }}
      >{stopping ? "Stopping…" : "Stop run"}</button>
    </div>
  );
}

function channelLabel(channel: Channel): string {
  return channel.title?.trim() || channel.identifier;
}

export function ChannelPicker({
  channels,
  selectedChannelId,
  onChannelSelect,
}: {
  channels: Channel[];
  selectedChannelId: number | null;
  onChannelSelect: (channelId: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const selected = channels.find((channel) => channel.id === selectedChannelId) ?? null;

  useEffect(() => {
    if (!open) return;
    const selectedIndex = Math.max(0, channels.findIndex((channel) => channel.id === selectedChannelId));
    menuRef.current?.querySelectorAll<HTMLButtonElement>("[role=option]")[selectedIndex]?.focus();
    const onPointer = (event: PointerEvent) => {
      if (
        !menuRef.current?.contains(event.target as Node)
        && !triggerRef.current?.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, channels, selectedChannelId]);

  const moveFocus = (delta: number) => {
    const options = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("[role=option]") ?? []);
    if (options.length === 0) return;
    const at = options.indexOf(document.activeElement as HTMLButtonElement);
    const next = (at + delta + options.length) % options.length;
    options[next].focus();
  };

  return (
    <div className="studio-channel-field">
      <span className="studio-channel-caption" id="studio-channel-caption">Channel</span>
      <button
        ref={triggerRef}
        type="button"
        className="studio-channel-trigger ui-dropdown-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls="studio-channel-menu"
        aria-label={selected ? `Studio channel: ${channelLabel(selected)}` : "Studio channel"}
        onClick={() => setOpen((current) => !current)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
            if (!open) {
              event.preventDefault();
              setOpen(true);
            }
          }
        }}
      >
        <span className="studio-channel-value" id="studio-channel-value">{selected ? channelLabel(selected) : "Choose a channel"}</span>
        <span className="studio-channel-chevron ui-chevron" aria-hidden="true" />
      </button>
      {open && (
        <div
          ref={menuRef}
          id="studio-channel-menu"
          className="studio-channel-menu ui-dropdown-menu"
          role="listbox"
          aria-label="Studio channel"
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") { event.preventDefault(); moveFocus(1); }
            else if (event.key === "ArrowUp") { event.preventDefault(); moveFocus(-1); }
            else if (event.key === "Home") { event.preventDefault(); menuRef.current?.querySelector<HTMLButtonElement>("[role=option]")?.focus(); }
            else if (event.key === "End") { event.preventDefault(); const items = menuRef.current?.querySelectorAll<HTMLButtonElement>("[role=option]"); items?.[items.length - 1]?.focus(); }
          }}
        >
          {channels.map((channel) => (
            <button
              key={channel.id}
              type="button"
              role="option"
              aria-selected={channel.id === selectedChannelId}
              className={`studio-channel-option ui-dropdown-option${channel.id === selectedChannelId ? " is-selected" : ""}`}
              title={channel.identifier}
              onClick={() => {
                setOpen(false);
                triggerRef.current?.focus();
                onChannelSelect(channel.id);
              }}
            >
              <span className="studio-channel-option-name">{channelLabel(channel)}</span>
              <span className="studio-channel-option-id">{channel.identifier}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ConversationRail({
  channels,
  selectedChannelId,
  conversations,
  selected,
  onSelect,
  onNew,
  onChannelSelect,
}: {
  channels: Channel[];
  selectedChannelId: number | null;
  conversations: Conversation[];
  selected: Conversation | null;
  onSelect: (conversation: Conversation) => void;
  onNew: () => void;
  onChannelSelect: (channelId: number) => void;
}) {
  return (
    <aside className="studio-rail" aria-label="Studio conversations">
      <div className="studio-rail-head"><h2 className="studio-rail-title">Conversations</h2></div>
      <ChannelPicker channels={channels} selectedChannelId={selectedChannelId} onChannelSelect={onChannelSelect} />
      <select
        className="studio-mobile-conversation-select"
        aria-label="Studio conversation"
        value={selected?.id ?? ""}
        onChange={(event) => {
          const conversation = conversations.find((item) => item.id === event.target.value);
          if (conversation) onSelect(conversation);
        }}
      >
        {!selected && <option value="">Choose a conversation</option>}
        {conversations.map((conversation) => <option key={conversation.id} value={conversation.id}>{conversation.title}</option>)}
      </select>
      <button className="studio-new" type="button" onClick={onNew}>
        <span aria-hidden="true">＋</span> New conversation
      </button>
      <div className="studio-conversations" role="list" aria-label="Conversations">
        {conversations.length === 0 ? (
          <p className="studio-empty-rail">Your working threads will appear here.</p>
        ) : (
          conversations.map((conversation) => (
            <div className={`studio-conversation-row ${selected?.id === conversation.id ? "is-selected" : ""}`} key={conversation.id} role="listitem">
              <button
                className="studio-conversation"
                type="button"
                title={conversation.title}
                aria-current={selected?.id === conversation.id ? "page" : undefined}
                onClick={() => onSelect(conversation)}
              >
                <span className="studio-conversation-mark" aria-hidden="true" />
                <span className="studio-conversation-copy">
                  <strong>{conversation.title}</strong>
                  <small>{conversation.channel_identifier || conversation.channel_title}</small>
                </span>
              </button>
            </div>
          ))
        )}
      </div>
      <div className="studio-rail-foot"><span>Channel context stays separate</span></div>
    </aside>
  );
}

export function StudioSettings({ channelId, channelLabel, onClose }: { channelId: number; channelLabel: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  // Modal isolation: Tab cycles inside the System Prompt dialog.
  useDialogFocusTrap(dialog);
  const [prompt, setPrompt] = useState("");
  const [initialPrompt, setInitialPrompt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
    let alive = true;
    const controller = new AbortController();
    void api<{ channel_id: number; system_prompt: string }>(`/studio/api/settings?channel_id=${encodeURIComponent(channelId)}`, { signal: controller.signal }).then(value => {
      if (alive) { setPrompt(value.system_prompt); setInitialPrompt(value.system_prompt); setLoading(false); }
    }).catch((error: unknown) => {
      if (!alive || (error instanceof DOMException && error.name === "AbortError")) return;
      setNotice("Could not load the system prompt. Close and try again.");
    });
    return () => { alive = false; controller.abort(); };
  }, [channelId]);
  // Channel-scoped and separately named: edits here never leave this
  // channel, and closing with unsaved edits asks first. The server is
  // checked against the loaded text, so conflicts keep the local text.
  const dirty = initialPrompt !== null && prompt !== initialPrompt;
  const closeWithConfirm = () => {
    if (dirty && !window.confirm("Discard unsaved System Prompt changes?")) return;
    onClose();
  };
  const save = async () => {
    setSaving(true); setNotice("");
    try {
      await api("/studio/api/settings", { method: "PATCH", headers: { "content-type": "application/json", "x-csrf-token": csrfToken() }, body: JSON.stringify({ channel_id: channelId, system_prompt: prompt, expected_prompt: initialPrompt }) });
      setInitialPrompt(prompt);
      setNotice(`Saved for ${channelLabel}. It applies to this channel's conversations only.`);
    } catch (error) { setNotice(error instanceof StudioApiError && error.status === 409
      ? "System Prompt changed in another tab. Your text is preserved; copy it before reopening to load the saved instructions."
      : "Could not save. Your text is preserved; try again."); }
    finally { setSaving(false); }
  };
  const clear = () => {
    if (prompt !== "" && !window.confirm("Clear the System Prompt? Save to apply the empty prompt.")) return;
    setPrompt("");
    setNotice("Cleared locally. Save to apply the empty prompt.");
  };
  return <dialog className="studio-settings" ref={dialog} aria-labelledby="studio-settings-title" onCancel={(event) => { event.preventDefault(); closeWithConfirm(); }} onClose={onClose}>
    <header><h2 id="studio-settings-title">System Prompt</h2><button type="button" aria-label="Close System Prompt" onClick={closeWithConfirm}>Close</button></header>
    <label htmlFor="studio-system-prompt">System Prompt</label>
    <p>Standing instructions for <strong>{channelLabel}</strong>: voice, editorial preferences, topics and source criteria. They never apply to another channel. Security rules still apply.</p>
    <textarea id="studio-system-prompt" autoFocus value={prompt} maxLength={12000} disabled={loading} onChange={event => { setPrompt(event.target.value); setNotice(""); }} placeholder="How should the Studio agent work with you?" />
    <footer><span>{prompt.length.toLocaleString()} / 12,000 · Leave empty to use defaults.{dirty ? " Unsaved changes." : ""}</span><span className="studio-settings-actions"><button type="button" disabled={loading || saving || prompt === ""} onClick={clear}>Clear</button><button type="button" className="studio-copy" disabled={loading || saving} onClick={() => void save()}>{saving ? "Saving…" : "Save instructions"}</button></span></footer>
    {notice && <p role="status">{notice}</p>}
  </dialog>;
}

/**
 * Formatted Full post preview. Uses the server-rendered sanitized Telegram
 * HTML when available, otherwise the canonical Markdown conversion shared
 * with clipboard HTML. The output is already escaped/sanitized upstream, so
 * it is safe to inject; commentary and source-review notes never enter the
 * publishable post field.
 */
function DraftPreview({ preview }: { preview: DraftPreview }) {
  if (!preview.html) return <p />;
  return <div className="studio-markdown" dangerouslySetInnerHTML={{ __html: preview.html }} />;
}

export const DRAFT_CHARACTER_LIMIT = 4096;
export const DRAFT_WARNING_THRESHOLD = 3800;

export function draftPlainCharacterCount(body: string): number {
  // JavaScript string length is UTF-16 code units, matching Telegram's
  // message limit and the server-side count.
  return plainFromMarkdown(body).length;
}

export function draftCounterState(body: string, serverCount?: number | null): { count: number; overLimit: boolean; warning: boolean } {
  const count = serverCount ?? draftPlainCharacterCount(body);
  return { count, overLimit: count > DRAFT_CHARACTER_LIMIT, warning: count >= DRAFT_WARNING_THRESHOLD };
}

export function draftSaveErrorMessage(error: unknown): string {
  if (error instanceof StudioApiError) {
    return error.payload.error?.message ?? error.message;
  }
  if (error instanceof Error && error.message.trim()) return error.message;
  return "Could not save this draft. Try again.";
}

export function DraftPanel({
  conversationId,
  seedDraft,
  open,
  onClose,
  onSelectConversation,
  watchForAgentChanges,
  refreshToken,
}: {
  conversationId: string | null;
  seedDraft: Draft | null;
  open: boolean;
  onClose: () => void;
  onSelectConversation: (conversationId: string) => void;
  watchForAgentChanges: boolean;
  refreshToken: number;
}) {
  const [draft, setDraft] = useState<Draft | null>(seedDraft);
  const [panelWidth, setPanelWidth] = useState(() => {
    try { return Math.max(320, Math.min(720, Number(localStorage.getItem("studio-artifact-width")) || 360)); }
    catch { return 360; }
  });
  useEffect(() => {
    document.documentElement.style.setProperty("--studio-draft-width", `${panelWidth}px`);
    try { localStorage.setItem("studio-artifact-width", String(panelWidth)); } catch { /* Optional preference. */ }
  }, [panelWidth]);
  const [versions, setVersions] = useState<DraftVersion[]>([]);
  const [saveState, setSaveState] = useState<"saved" | "unsaved" | "saving" | "conflict" | "error">("saved");
  const [saveError, setSaveError] = useState("");
  const [copied, setCopied] = useState(false);
  const [copyNote, setCopyNote] = useState("");
  const [scheduleDraft, setScheduleDraft] = useState<Draft | null>(null);
  const [scheduling, setScheduling] = useState(false);
  const [imageUploading, setImageUploading] = useState(false);
  // The artifact opens formatted (Full post preview, including the heading);
  // raw-source editing is an explicit Edit action, never the default.
  const [previewOpen, setPreviewOpen] = useState(true);
  const [conflict, setConflict] = useState<{ server: Draft; local: Draft } | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  // Selecting a version in the selector shows it read-only; only Choose makes
  // it the current version (a pointer move, never a copy).
  const viewedVersion = versions.find((item) => item.version === selectedVersion);
  const viewingOld = !!(draft && viewedVersion && selectedVersion !== draft.current_version);
  const shownBody = viewingOld && viewedVersion ? viewedVersion.body : (draft?.body ?? "");
  const shownCounter = draftCounterState(shownBody, viewingOld ? viewedVersion?.character_count : draft?.character_count);
  const canSave = saveState === "unsaved" || saveState === "error";
  const isBlankBody = isBlankDraftBody(shownBody);
  const isCopyBlocked = shownCounter.overLimit || isBlankBody;
  // Version views and unsaved edits render through the canonical Markdown
  // conversion; a clean saved draft prefers its server-rendered Telegram HTML.
  const preview = draftPreviewHtml(shownBody, viewingOld ? null : draft, saveState === "unsaved");
  const hydrated = useRef(false);
  const draftRef = useRef<Draft | null>(seedDraft);
  const saveStateRef = useRef(saveState);
  const localChange = useRef(0);
  const loadedConversation = useRef<string | null>(conversationId);
  // Stale-read guard: every load carries its scope; delayed responses from a
  // previous conversation (or an older refetch) are ignored, and the in-flight
  // request is aborted on conversation change.
  const loadSequence = useRef(0);
  const loadScope = useRef<StudioScope>({ channelId: null, conversationId, sequence: 0 });
  const loadAbort = useRef<AbortController | null>(null);

  useEffect(() => { draftRef.current = draft; }, [draft]);
  useEffect(() => { saveStateRef.current = saveState; }, [saveState]);

  const load = () => {
    const conversationChanged = loadedConversation.current !== conversationId;
    const previousConversation = loadedConversation.current;
    if (conversationChanged && saveStateRef.current === "unsaved") {
      if (!window.confirm("Discard unsaved draft changes?")) {
        if (previousConversation) onSelectConversation(previousConversation);
        return Promise.resolve();
      }
    }
    loadedConversation.current = conversationId;
    loadAbort.current?.abort();
    const controller = new AbortController();
    loadAbort.current = controller;
    const scope: StudioScope = {
      channelId: null,
      conversationId,
      sequence: ++loadSequence.current,
    };
    loadScope.current = scope;
    if (!conversationId) {
      setDraft(null);
      setVersions([]);
      return Promise.resolve();
    }
    hydrated.current = false;
    if (conversationChanged) {
      localChange.current = 0;
      setScheduleDraft(null);
      setScheduling(false);
      setConflict(null);
      setSaveState("saved");
      setSaveError("");
      setCopyNote("");
      setCopied(false);
      // A newly opened conversation starts formatted; edits are explicit.
      setPreviewOpen(true);
    }
    return api<{ draft: Draft | null }>(`/studio/api/conversations/${conversationId}/draft`, { signal: controller.signal })
      .then((payload) => {
        if (isStaleScope(scope, loadScope.current) || loadedConversation.current !== conversationId) return;
        setDraft((current) => {
          if (!conversationChanged && current && localChange.current > 0) return current;
          return payload.draft;
        });
        if (payload.draft) {
          const draftScope = scope;
          const draftId = payload.draft.id;
          setSelectedVersion(payload.draft.current_version);
          return api<{ versions: DraftVersion[] }>(`/studio/api/drafts/${draftId}/versions`, { signal: controller.signal }).then((history) => {
            if (!isStaleScope(draftScope, loadScope.current)) setVersions(history.versions);
          });
        }
        setVersions([]);
        setSelectedVersion(null);
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (!isStaleScope(scope, loadScope.current)) setSaveState("error");
      })
      .finally(() => {
        if (!isStaleScope(scope, loadScope.current)) hydrated.current = true;
      });
  };

  useEffect(() => {
    void load().catch(() => setSaveState("error"));
    // A new conversation or completed agent run requires one authoritative
    // refresh. Continuous polling is handled separately and only while an
    // agent run can actually be writing draft versions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, refreshToken]);

  useEffect(() => {
    if (!conversationId || !watchForAgentChanges) return;
    const poll = window.setInterval(() => {
      if (document.visibilityState === "hidden" || !hydrated.current || saveStateRef.current === "saving") return;
      void api<{ draft: Draft | null }>(`/studio/api/conversations/${conversationId}/draft`).then((payload) => {
        // Ignore delayed responses once the conversation moved on.
        if (loadedConversation.current !== conversationId || loadScope.current.conversationId !== conversationId) return;
        const next = payload.draft;
        const previous = draftRef.current;
        if (next && (!previous || previous.id !== next.id || previous.current_version !== next.current_version || previous.updated_at !== next.updated_at)) {
          void api<{ versions: DraftVersion[] }>(`/studio/api/drafts/${next.id}/versions`).then((history) => setVersions(history.versions)).catch(() => undefined);
        }
        if (next && !(previous && localChange.current > 0)) setSelectedVersion(next.current_version);
        setDraft((current) => {
          if (!next) return current;
          // Preserve unsaved local typing while a streamed agent run writes a
          // new server version.
          if (current && localChange.current > 0) return current;
          return next;
        });
      }).catch(() => undefined);
    }, 1800);
    return () => window.clearInterval(poll);
  }, [conversationId, watchForAgentChanges]);

  const save = (local: Draft, changeId: number, newVersion = true) => {
    setSaveState("saving");
    void api<{ draft: Draft }>(`/studio/api/drafts/${local.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ expected_revision: local.revision, body: local.body, working_title: local.working_title, source_links: local.source_links || [], media_ids: local.media_ids || [], save_as_new_version: newVersion }),
    })
      .then((payload) => {
        if (loadedConversation.current !== local.conversation_id) return;
        const latest = localChange.current === changeId;
        // Every save appends a version. Refetch the authoritative history.
        void api<{ versions: DraftVersion[] }>(`/studio/api/drafts/${payload.draft.id}/versions`).then((history) => setVersions(history.versions)).catch(() => undefined);
        setSelectedVersion(payload.draft.current_version);
        setDraft((current) => {
          if (!current || localChange.current === changeId) {
            if (localChange.current === changeId) localChange.current = 0;
            return payload.draft;
          }
          return { ...current, revision: payload.draft.revision };
        });
        if (latest) setSaveState("saved");
        setSaveError("");
        setConflict(null);
      })
      .catch((error: unknown) => {
        if (error instanceof StudioApiError && error.status === 409 && error.payload.server_draft) {
          setConflict({ server: error.payload.server_draft, local });
          setSaveError("");
          setSaveState("conflict");
        } else {
          setSaveError(draftSaveErrorMessage(error));
          setSaveState("error");
        }
      });
  };

  // Edits stay local until the owner presses Save;
  // nothing is written, and no version is created, on its own.
  useEffect(() => {
    if (saveState !== "unsaved") return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [saveState]);

  const prepareSchedule = async () => {
    if (!draft || viewingOld || scheduling || imageUploading || saveState === "saving" || saveState === "conflict") return;
    setScheduling(true);
    try {
      let selected = draft;
      if (saveState !== "saved") {
        const result = await api<{ draft: Draft }>(`/studio/api/drafts/${draft.id}`, { method: "PATCH", headers: { "content-type": "application/json", "x-csrf-token": csrfToken() }, body: JSON.stringify({ expected_revision: draft.revision, body: draft.body, working_title: draft.working_title, source_links: draft.source_links || [], media_ids: draft.media_ids || [], save_as_new_version: true }) });
        selected = result.draft;
        if (loadedConversation.current !== draft.conversation_id) return;
        setDraft(selected); localChange.current = 0; setSaveState("saved"); setSelectedVersion(selected.current_version);
        const history = await api<{ versions: DraftVersion[] }>(`/studio/api/drafts/${selected.id}/versions`);
        if (loadedConversation.current !== draft.conversation_id) return;
        setVersions(history.versions);
      }
      if (loadedConversation.current === draft.conversation_id) setScheduleDraft(selected);
    } catch (e) { if (loadedConversation.current === draft.conversation_id) setSaveError(draftSaveErrorMessage(e)); }
    finally { if (loadedConversation.current === draft.conversation_id) setScheduling(false); }
  };

  const saveNow = (newVersion: boolean) => {
    if (!draft) return;
    save(draft, localChange.current, newVersion);
  };

  const edit = (field: "body" | "working_title", value: string) => {
    localChange.current += 1;
    setDraft((current) => {
      if (!current) return current;
      if (field === "working_title") {
        return { ...current, working_title: value.slice(0, 240), copied_at: null };
      }
      const plain = plainFromMarkdown(value);
      return {
        ...current,
        body: value,
        body_plain: plain,
        // The server-rendered HTML belongs to the previous body; until the
        // edit is saved, a rich copy must fall back to plain text.
        body_html: undefined,
        character_count: plain.length,
        plain_character_count: plain.length,
        over_limit: plain.length > 4096,
        warning_threshold: plain.length >= 3800,
        copied_at: null,
      };
    });
    setSaveState("unsaved");
    setSaveError("");
    setCopied(false);
  };

  const copy = async () => {
    // Blank or over-limit bodies never reach the clipboard: the button is
    // disabled in both cases and this guard covers programmatic callers.
    if (!draft || shownCounter.overLimit || isBlankDraftBody(shownBody)) return;
    // Both flavours come from the Markdown shown in the editor (the current
    // draft, or the version being viewed), saved or not.
    // text/plain carries Telegram's own markup (**bold**, __italic__), which
    // every Telegram app converts on send even when it ignores rich flavours;
    // text/html carries real tags and links for clients that read them.
    const plain = telegramMarkupFromMarkdown(shownBody);
    const html = htmlFromMarkdown(shownBody);
    try {
      // 1. Copy-event handler: both flavours under our control, synchronous
      //    inside the click, no permission prompt.
      // 2. Rendered selection: browser-serialised flavours (adds RTF in Safari).
      // 3. Async Clipboard API; last because Safari rejects it after an await
      //    and plain-HTTP origins do not offer it.
      let done = copyRichText(plain, html) || copyRenderedSelection(html);
      let rich = done;
      if (!done) {
        const clip = navigator.clipboard as unknown as { write?: (items: unknown[]) => Promise<void>; writeText?: (text: string) => Promise<void> } | undefined;
        const ClipboardItemCtor = (window as unknown as { ClipboardItem?: new (items: Record<string, Blob>) => unknown }).ClipboardItem;
        if (clip?.write && ClipboardItemCtor) {
          await clip.write([new ClipboardItemCtor({ "text/plain": new Blob([plain], { type: "text/plain" }), "text/html": new Blob([html], { type: "text/html" }) })]);
          done = rich = true;
        } else if (clip?.writeText) {
          await clip.writeText(plain);
          done = true;
        }
      }
      if (!done) throw new Error("Clipboard unavailable");
      setCopied(true);
      // Say which flavour landed so a plain-text paste can be diagnosed at once.
      setCopyNote(rich ? "Copied. Formatting travels as rich text and as Telegram markup." : "Copied as Telegram markup only (this browser blocks rich copy).");
      setDraft((current) => current ? { ...current, copied_at: new Date().toISOString() } : current);
      window.setTimeout(() => { setCopied(false); setCopyNote(""); }, 3200);
      // Record the copy on the saved revision; failures here must not undo a successful copy.
      if (saveState === "saved" && !viewingOld) void api(`/studio/api/drafts/${draft.id}/copied`, { method: "POST", headers: { "x-csrf-token": csrfToken() } }).catch(() => undefined);
    } catch {
      setCopyNote("Clipboard unavailable — select the text and copy manually");
    }
  };

  const choose = (version: DraftVersion) => {
    if (!draft) return;
    if (saveState === "unsaved" && !window.confirm(`Discard unsaved changes and make v${version.version} the current version?`)) return;
    setSaveState("saving");
    void api<{ draft: Draft }>(`/studio/api/drafts/${draft.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ expected_revision: draft.revision, choose_version: version.version }),
    }).then((payload) => {
      // Choosing moves the current pointer; no version is created.
      localChange.current = 0;
      setDraft(payload.draft);
      setSelectedVersion(payload.draft.current_version);
      setSaveState("saved");
      setSaveError("");
      setConflict(null);
    }).catch((error: unknown) => {
      if (error instanceof StudioApiError && error.status === 409 && error.payload.server_draft) {
        setConflict({ server: error.payload.server_draft, local: draft });
        setSaveState("conflict");
      } else {
        setSaveError(draftSaveErrorMessage(error));
        setSaveState("error");
      }
    });
  };

  const keepLocal = () => {
    if (!conflict) return;
    localChange.current += 1;
    // Adopt the server revision so the next Save is accepted, keep the text.
    setDraft({ ...conflict.server, body: conflict.local.body, working_title: conflict.local.working_title, source_links: conflict.local.source_links, media_ids: conflict.local.media_ids });
    setConflict(null);
    setSaveState("unsaved");
    setSaveError("");
  };

  const useServer = () => {
    if (!conflict) return;
    localChange.current = 0;
    setDraft(conflict.server);
    setConflict(null);
    setSaveState("saved");
    setSaveError("");
  };

  const shownLinks = (viewingOld ? viewedVersion?.source_links : draft?.source_links) || [];
  const clickableSources = shownLinks.filter((url) => {
    try { const parsed = new URL(url); return ["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password; }
    catch { return false; }
  });

  return (
    <aside className={`studio-draft ${open ? "is-open" : ""}`} aria-label="Draft artifact">
      <div className="studio-draft-resizer" role="separator" aria-label="Resize draft panel" aria-orientation="vertical" aria-valuemin={320} aria-valuemax={720} aria-valuenow={panelWidth} tabIndex={0}
        onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); }}
        onPointerMove={(event) => { if (event.currentTarget.hasPointerCapture(event.pointerId)) setPanelWidth(Math.max(320, Math.min(720, window.innerWidth - event.clientX))); }}
        onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}
        onKeyDown={(event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); setPanelWidth((width) => event.key === "Home" ? 320 : event.key === "End" ? 720 : Math.max(320, Math.min(720, width + (event.key === "ArrowLeft" ? 20 : -20)))); } }}
        onDoubleClick={() => setPanelWidth(360)} />
      <div className="studio-panel-heading">
        <div>
          <p className="studio-overline">Artifact</p>
          <h2>Draft workspace</h2>
        </div>
        <div className="studio-panel-actions">
          <span className={`studio-save-state is-${saveState}`} role="status">{saveState === "saving" ? "Saving…" : saveState === "unsaved" ? "Unsaved changes" : saveState === "conflict" ? "Needs review" : saveState === "error" ? "Retry needed" : "Saved"}</span>
          <button type="button" className="studio-draft-close" onClick={onClose}>Back to chat</button>
        </div>
      </div>
      {!draft ? (
        <div className="studio-draft-empty">
          <div className="studio-draft-glyph" aria-hidden="true">∿</div>
          <h3>Your draft will live here</h3>
          <p>Ask the agent to find a story and shape a source-backed, Telegram-ready post. The artifact stays editable while the conversation continues.</p>
        </div>
      ) : (
        <div className="studio-draft-content">
          <label className="studio-draft-title">Artifact title<input aria-label="Artifact title" value={viewingOld ? viewedVersion?.working_title || "Title unavailable for this older version" : draft.working_title} disabled={viewingOld || scheduling} onChange={(event) => edit("working_title", event.target.value)} maxLength={240} placeholder="Untitled draft" /><span className="studio-draft-title-hint">Draft name. The publication text is below.</span></label>
          {viewingOld && <p className="studio-draft-viewing" role="status">Viewing v{selectedVersion} (read-only). Restore makes it the current version.</p>}
          <ImageAttachments key={draft.id} onBusyChange={setImageUploading} draftId={draft.id} ids={viewingOld ? viewedVersion?.media_ids || [] : draft.media_ids || []} version={viewingOld ? viewedVersion?.version : undefined} disabled={viewingOld || scheduling || imageUploading || saveState === "saving"} onChange={(ids) => { localChange.current += 1; setDraft(current => current ? { ...current, media_ids: ids } : current); setSaveState("unsaved"); setSaveError(""); }} />
          {previewOpen
            ? <div className="studio-draft-preview" role="region" aria-label="Full post preview"><DraftPreview preview={preview} /></div>
            : <textarea className="studio-draft-editor" aria-label="Full post — headline and body" value={shownBody} readOnly={viewingOld || scheduling} onChange={(event) => edit("body", event.target.value)} />}
          <div className={`studio-char-count ${shownCounter.overLimit ? "is-over" : shownCounter.warning ? "is-warning" : ""}`}>
            <span>{shownCounter.count.toLocaleString()} / {DRAFT_CHARACTER_LIMIT.toLocaleString()} Telegram characters</span>
            <span>{shownCounter.overLimit ? "Copy blocked · over Telegram limit" : isBlankBody ? "Copy blocked · post is blank" : shownCounter.warning ? "Near Telegram limit" : "Telegram ready"}</span>
          </div>
          {conflict && <div className="studio-conflict" role="alert"><strong>This draft changed elsewhere.</strong><span>Your local text is preserved.</span><div><button type="button" onClick={keepLocal}>Keep my text</button><button type="button" onClick={useServer}>Use server version</button></div></div>}
          {clickableSources.length > 0 && <div className="studio-draft-notes"><strong>Sources</strong><div className="studio-source-chips">{clickableSources.map((url) => <a key={url} href={url} target="_blank" rel="noopener noreferrer">{new URL(url).hostname}</a>)}</div></div>}
          <label className="studio-draft-notes">Source links
            <textarea aria-label="Source links" value={shownLinks.join("\n")} readOnly={viewingOld || scheduling} placeholder="One URL per line" onChange={(event) => {
              localChange.current += 1;
              setDraft(current => current ? { ...current, source_links: event.target.value.split("\n") } : current);
              setSaveState("unsaved"); setSaveError("");
            }} />
          </label>
          <div className="studio-draft-toolbar"><button type="button" className="studio-draft-mode" aria-pressed={previewOpen} onClick={() => setPreviewOpen((open) => !open)}>{previewOpen ? "Edit post" : "Preview"}</button><span className="studio-draft-save-group"><button type="button" className="studio-draft-save" onClick={() => saveNow(true)} disabled={!canSave || viewingOld || scheduling || imageUploading} title="Save your edits as a new version">Save</button></span><label className="studio-version-select">Version<select aria-label="Draft version" value={selectedVersion ?? draft.current_version} onChange={(event) => setSelectedVersion(Number(event.target.value))}>{versions.map((version) => <option key={version.version} value={version.version}>v{version.version} · {version.origin}</option>)}</select><button type="button" className="studio-restore" onClick={() => { const version = versions.find((item) => item.version === selectedVersion); if (version && version.version !== draft.current_version) choose(version); }} disabled={selectedVersion === null || selectedVersion === draft.current_version || saveState === "saving"} title="Make the viewed version the current one (no copy is created)">Restore</button></label></div>
          {saveError && <p className="studio-save-error" role="alert">{saveError}</p>}
        </div>
      )}
      {scheduleDraft && <ScheduleDialog draft={scheduleDraft} onClose={() => setScheduleDraft(null)} onScheduled={() => { setScheduleDraft(null); setCopyNote("Queued for Telegram. Track its status in Calendar."); }} />}
      {draft && (
        <div className="studio-draft-bottom">
          <button type="button" className="studio-schedule" onClick={() => void prepareSchedule()} disabled={viewingOld || scheduling || imageUploading || saveState === "saving" || saveState === "conflict" || shownCounter.overLimit || (isBlankBody && !draft.media_ids?.length)}>{scheduling ? "Saving…" : "Schedule"}</button>
          <button
            type="button"
            className="studio-copy studio-copy-sticky"
            onClick={() => void copy()}
            disabled={isCopyBlocked}
            title={shownCounter.overLimit ? "Shorten the post to copy it" : isBlankBody ? "Write the post before copying" : "Copy the full post for Telegram"}
          >{copied ? "Copied" : "Copy full post"}</button>
          {copyNote && <span className="studio-copy-note" role="status">{copyNote}</span>}
        </div>
      )}
    </aside>
  );
}

function ProfilePrimer({ bootstrap, onBootstrap }: { bootstrap: Bootstrap; onBootstrap: (next: Bootstrap) => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const consent = bootstrap.consent;
  const grant = () => {
    setBusy(true);
    setMessage("");
    void api<{ consent: Bootstrap["consent"]; profile: Bootstrap["profile"] }>("/studio/api/consent", {
      method: "POST",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({
        confirm: true,
        configuration_fingerprint: consent.configuration_fingerprint,
        channel_id: bootstrap.selected_channel_id,
      }),
    })
      .then(() =>
        // Consent changes what the primer shows, so reload the bootstrap
        // payload instead of leaving the consent card on screen.
        api<Bootstrap>(`/studio/api/bootstrap?channel_id=${encodeURIComponent(bootstrap.selected_channel_id ?? "")}`)
          .then((next) => onBootstrap(next))
          .catch(() => setMessage("Consent granted. Reload the page to continue.")),
      )
      .catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Consent could not be saved."))
      .finally(() => setBusy(false));
  };

  if (consent.required && !consent.granted) {
    return (
      <section className="studio-primer studio-primer-consent" aria-label="Provider consent">
        <div>
          <p className="studio-overline">One-time permission</p>
          <h2>{consent.disclosure?.title ?? "Allow OpenRouter for Studio"}</h2>
          <p>{consent.disclosure?.message ?? "Studio sends bounded channel evidence to OpenRouter for agent assistance."}</p>
        </div>
        <button type="button" className="studio-primer-action" onClick={grant} disabled={busy}>{busy ? "Saving…" : "Allow and analyze"}</button>
        {message && <p className="studio-primer-error" role="alert">{message}</p>}
      </section>
    );
  }
  return null;
}

function formatRunDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "—";
  if (durationMs < 1000) return `${durationMs} ms`;
  const seconds = durationMs / 1000;
  return seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function formatRunTokens(usage: RunUsage | undefined): string {
  if (!usage) return "—";
  return usage.total_tokens.toLocaleString();
}

function RunDetailsPanel({ run }: { run: RunSummary | null }) {
  const [open, setOpen] = useState(false);
  const [details, setDetails] = useState<RunDetails | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    if (!run) return;
    setLoading(true);
    setError("");
    void api<{ run: RunSummary; details: RunDetails }>(`/studio/api/runs/${run.id}/details`)
      .then((payload) => setDetails(payload.details))
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Run details could not load."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    setDetails(null);
    setError("");
    if (open && run) load();
    // The panel is intentionally fetched only when opened. Run metadata is
    // operational context, not part of the main chat payload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, open]);

  if (!run) return null;
  const shown = details ?? {
    provider: run.provider,
    requested_model: run.requested_model,
    actual_model: run.actual_model,
    prompt_version: "—",
    status: run.status,
    stage: run.stage,
    duration_ms: run.duration_ms,
    usage: run.usage,
    activity: { event_count: 0, tool_call_count: 0, result_count: 0, source_count: 0, story_count: 0, cache_hit_count: 0, degraded: false, references: {} },
    error: run.error_code ? { code: run.error_code, message: run.error_message ?? "The run could not complete.", retryable: false } : null,
  };
  const cost = shown.usage.estimated_cost_usd;

  return (
    <section className={`studio-run-details ${open ? "is-open" : ""}`} aria-label="Run details">
      <button
        type="button"
        className="studio-run-details-toggle"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span><span className="studio-run-details-mark" aria-hidden="true" />Run details</span>
        <small>{run.status} · {formatRunDuration(shown.duration_ms)}</small>
        <span className="studio-run-details-chevron ui-chevron" aria-hidden="true" />
      </button>
      {open && (
        <div className="studio-run-details-body">
          {loading && <span className="studio-run-details-muted" role="status">Loading operational summary…</span>}
          {error && <span className="studio-run-details-error" role="alert">{error}</span>}
          {!loading && !error && (
            <>
              <div className="studio-run-meta-grid">
                <div><span>Provider</span><strong>{shown.provider}</strong></div>
                <div><span>Model</span><strong>{shown.actual_model || shown.requested_model}</strong></div>
                <div><span>Duration</span><strong>{formatRunDuration(shown.duration_ms)}</strong></div>
                <div><span>Tokens</span><strong>{formatRunTokens(shown.usage)}</strong></div>
                <div><span>Estimated cost</span><strong>{cost === undefined ? "Unavailable" : `$${cost.toFixed(4)}`}</strong></div>
                <div><span>Tool calls</span><strong>{shown.activity.tool_call_count}</strong></div>
              </div>
              <div className="studio-run-details-foot">
                <span>{shown.activity.source_count} sources · {shown.activity.story_count} stories</span>
                <span>{shown.activity.cache_hit_count} cache hits · {shown.prompt_version}</span>
              </div>
              {(() => {
                const searchNotice = describeSearchOutcome(shown.activity);
                return searchNotice !== null && <span className="studio-run-details-warning">{searchNotice}</span>;
              })()}
              {shown.error && <div className="studio-run-details-error"><strong>{shown.error.code}</strong><span>{shown.error.message}</span></div>}
            </>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * Explorer-to-Studio handoff: writes the referenced post's prompt text into
 * the composer without sending. Composer content the owner already typed is
 * never overwritten; the caller surfaces a notice instead.
 */
function ComposerPrefillBridge({
  pendingPrefill,
  onPrefillResult,
}: {
  pendingPrefill: StudioPrefill | null;
  onPrefillResult: (applied: boolean, prefill: StudioPrefill) => void;
}) {
  const { value, setText } = unstable_useComposerInput();
  const seen = useRef<string | null>(null);
  useEffect(() => {
    if (!pendingPrefill) return;
    const key = `${pendingPrefill.channel_id}:${String(pendingPrefill.post_id ?? "")}:${pendingPrefill.text.length}:${pendingPrefill.text.slice(0, 64)}`;
    if (seen.current === key) return;
    seen.current = key;
    if (value.trim()) {
      onPrefillResult(false, pendingPrefill);
    } else {
      setText(pendingPrefill.text);
      onPrefillResult(true, pendingPrefill);
    }
  }, [pendingPrefill, value, setText, onPrefillResult]);
  return null;
}

export function StudioThread({
  conversation,
  seedRun,
  consent,
  pendingPrefill,
  onPrefillResult,
  onStopRun,
  onRunActivityChange,
  onRunFinished,
}: {
  conversation: Conversation;
  seedRun: RunSummary | null;
  consent: Bootstrap["consent"];
  pendingPrefill: StudioPrefill | null;
  onPrefillResult: (applied: boolean, prefill: StudioPrefill) => void;
  onStopRun: (runId: string) => Promise<void>;
  onRunActivityChange: (active: boolean) => void;
  onRunFinished: () => void;
}) {
  const agent = useMemo(
    () =>
      new HttpAgent({
        url: "/studio/api/agent",
        threadId: conversation.id,
        headers: {
          "x-csrf-token": csrfToken(),
        },
        fetch: studioAgentFetch,
      }),
    [conversation.id],
  );
  const activeRunId = useRef<string | null>(seedRun?.id ?? null);
  const [, setMessages] = useState<PersistedMessage[]>([]);
  const retryPending = useRef(false);
  const retryScope = useRef(conversation.id);
  useEffect(() => {
    retryScope.current = conversation.id;
    return () => { retryScope.current = ""; };
  }, [conversation.id]);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState("");
  const [loading, setLoading] = useState(true);
  const [prefillNotice, setPrefillNotice] = useState("");
  const handlePrefillResult = useCallback((applied: boolean, prefill: StudioPrefill) => {
    setPrefillNotice(applied
      ? "Post reference added to the composer. Review it before sending."
      : "The composer already has text, so the post reference was not inserted. Copy it from the reader instead.");
    onPrefillResult(applied, prefill);
  }, [onPrefillResult]);
  const [recoveredRun, setRecoveredRun] = useState<RunSummary | null>(seedRun);
  const [recoveredEvents, setRecoveredEvents] = useState<RunEvent[]>([]);
  // Set when stored-event polling exhausts its retries. The live agent
  // subscription stays attached; Reconnect re-runs discovery so the stored
  // run, messages and draft reconcile and duplicate events stay suppressed.
  const [linkDown, setLinkDown] = useState(false);
  const [reconnectToken, setReconnectToken] = useState(0);
  const reconnect = useCallback(() => {
    setLinkDown(false);
    setReconnectToken((token) => token + 1);
  }, []);
  const markRunFailed = useCallback((error: unknown) => {
    const runId = activeRunId.current;
    if (!runId) return;
    const failure = describeRunFailure(error);
    setRecoveredRun((current) => ({
      ...(current ?? buildRunHint(conversation.id, runId, "failed")),
      status: "failed",
      stage: "failed",
      error_code: failure.code,
      error_message: failure.message,
      finished_at: new Date().toISOString(),
    }));
    onRunFinished();
  }, [conversation.id, onRunFinished]);
  const runtime = useAgUiRuntime({
    agent,
    showThinking: false,
    onError: markRunFailed,
  });

  const retry = useCallback(async () => {
    if (!recoveredRun || recoveredRun.status !== "failed" || retryPending.current || loading || runtime.thread.getState().isRunning) return;
    retryPending.current = true;
    setRetrying(true);
    setRetryError("");
    try {
      const payload = await api<{ messages: PersistedMessage[] }>(`/studio/api/conversations/${conversation.id}/messages`);
      if (retryScope.current !== conversation.id) return;
      const request = [...payload.messages].reverse().find((message) => message.role === "user");
      if (!request) throw new Error("The original request could not be found.");
      setMessages(payload.messages);
      runtime.thread.reset(asThreadMessages(payload.messages));
      // startRun reuses the persisted user message rather than the composer:
      // unsent text stays intact and the server verifies the failed run ID.
      runtime.thread.startRun({
        parentId: `persisted-${request.id}`,
        runConfig: { custom: { retryRunId: recoveredRun.id } },
      });
    } catch (error) {
      setRetryError(error instanceof Error ? error.message : "The request could not be restarted.");
    } finally {
      retryPending.current = false;
      setRetrying(false);
    }
  }, [conversation.id, loading, recoveredRun, runtime]);

  useEffect(() => {
    onRunActivityChange(recoveredRun?.status === "queued" || recoveredRun?.status === "running");
  }, [onRunActivityChange, recoveredRun?.status]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    void api<{ messages: PersistedMessage[] }>(`/studio/api/conversations/${conversation.id}/messages`)
      .then((payload) => {
        if (!alive) return;
        setMessages(payload.messages);
        runtime.thread.reset(asThreadMessages(payload.messages));
      })
      .catch((error: unknown) => {
        if (alive) console.error("Could not load Studio history", error);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [conversation.id, runtime]);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    let cursor = 0;
    if (seedRun) {
      activeRunId.current = seedRun.id;
      setRecoveredRun(seedRun);
    }

    const restoreMessages = () => api<{ messages: PersistedMessage[] }>(`/studio/api/conversations/${conversation.id}/messages`)
      .then((payload) => {
        if (!alive) return;
        setMessages(payload.messages);
        runtime.thread.reset(asThreadMessages(payload.messages));
      });

    let consecutiveErrors = 0;
    const poll = (run: RunSummary) => {
      void api<{ run: RunSummary; events: RunEvent[] }>(`/studio/api/runs/${run.id}/events?after=${cursor}`)
        .then((payload) => {
          if (!alive) return;
          consecutiveErrors = 0;
          // A delayed response for another conversation must not leak into
          // this thread after a switch.
          if (payload.run.conversation_id && payload.run.conversation_id !== conversation.id) return;
          setRecoveredRun(payload.run);
          if (payload.events.length > 0) {
            cursor = payload.events[payload.events.length - 1].sequence;
            setRecoveredEvents((current) => mergeRunEvents(current, payload.events));
          }
          if (payload.run.status === "queued" || payload.run.status === "running") {
            timer = window.setTimeout(() => poll(payload.run), 850);
          } else {
            void restoreMessages();
            onRunFinished();
          }
        })
        .catch((error: unknown) => {
          if (!alive) return;
          const status = error instanceof StudioApiError ? error.status : 0;
          if (isTerminalPollStatus(status)) {
            setRecoveredRun(null);
            onRunFinished();
            return;
          }
          consecutiveErrors += 1;
          if (shouldStopPollingAfterErrors(consecutiveErrors)) {
            setRecoveredRun(null);
            setLinkDown(true);
            onRunFinished();
            return;
          }
          const delay = nextPollDelay(consecutiveErrors);
          timer = window.setTimeout(() => poll(run), delay);
        });
    };

    setRecoveredEvents([]);
    setLinkDown(false);
    const subscription = agent.subscribe({
      onRunInitialized: ({ input }) => {
        if (!input.runId) return;
        activeRunId.current = input.runId;
        setLinkDown(false);
        setRecoveredEvents([]);
        setRecoveredRun(buildRunHint(conversation.id, input.runId, "queued"));
      },
      onRunStartedEvent: ({ event, input }) => {
        const canonicalRunId = event.runId || input.runId;
        if (!canonicalRunId) return;
        activeRunId.current = canonicalRunId;
        const hint = buildRunHint(conversation.id, canonicalRunId, "running");
        setRecoveredRun(hint);
        poll(hint);
      },
      onRunFailed: ({ error }) => markRunFailed(error),
    });
    const discover = seedRun
      ? Promise.resolve({ run: seedRun })
      : api<{ run: RunSummary | null }>(`/studio/api/conversations/${conversation.id}/active-run`);
    void discover.then((payload) => {
      if (!alive || !payload.run) return;
      setRecoveredRun(payload.run);
      poll(payload.run);
    }).catch(() => undefined);

    return () => {
      // Leaving the conversation only detaches local listeners and timers;
      // the server run keeps going and is reconciled on return. Cancellation
      // is exclusively server-confirmed through the Stop control, so a route
      // or conversation change never falsely reports a cancelled run.
      alive = false;
      subscription.unsubscribe();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [agent, conversation.id, markRunFailed, onRunFinished, runtime, seedRun?.id, reconnectToken]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="studio-thread-wrap">
        <ComposerPrefillBridge pendingPrefill={pendingPrefill} onPrefillResult={handlePrefillResult} />
        {loading && <div className="studio-loading" role="status">Restoring this conversation…</div>}
        <RunDetailsPanel run={recoveredRun} />
        {recoveredRun?.status === "failed" && (
          <div className="studio-run-error" role="alert">
            <strong>{recoveredRun.error_message ?? "The agent could not complete this run."}</strong>
            <span>Try again with the same request. Your saved draft and chat history are kept.</span>
            {retryError && <span role="alert">{retryError}</span>}
            <div className="studio-run-error-actions">
              <button type="button" disabled={retrying || loading} onClick={() => void retry()}>{retrying ? "Restarting…" : "Try again"}</button>
              {isSetupBlockerCode(recoveredRun.error_code) && <a href="/settings">Check settings</a>}
            </div>
          </div>
        )}
        {linkDown && (
          <div className="studio-link-down" role="alert">
            <span>Connection lost while following this run. Stored messages are kept.</span>
            <button type="button" onClick={reconnect}>Reconnect</button>
          </div>
        )}
        <ThreadPrimitive.Root className="studio-thread">
          <ThreadPrimitive.Viewport className="studio-viewport">
            {!loading && (
              <ThreadPrimitive.Empty>
              <div className="studio-welcome">
                <span className="studio-welcome-kicker">Ready when you are</span>
                <h2>What should we make clearer today?</h2>
                <p>I’ll start with the selected channel’s history and keep the working context in this conversation.</p>
                <div className="studio-prompts" aria-label="Suggested prompts">
                  <button type="button" onClick={() => { runtime.thread.composer.setText("Show me what performs best"); focusComposer(); }}>Show me what performs best</button>
                  <button type="button" onClick={() => { runtime.thread.composer.setText("Help me find a fresh angle"); focusComposer(); }}>Help me find a fresh angle</button>
                </div>
              </div>
              </ThreadPrimitive.Empty>
            )}
            <ThreadPrimitive.Messages components={{ Message: StudioMessage }} />
          </ThreadPrimitive.Viewport>
          <div className="studio-composer-stack">
            {prefillNotice && <p className="studio-prefill-notice" role="status">{prefillNotice}</p>}
            <AgentActivity run={recoveredRun} events={recoveredEvents} onStopRun={onStopRun} />
            <ComposerPrimitive.Root className="studio-composer">
              <ComposerPrimitive.Input
                aria-label="Message the Studio agent"
                placeholder="Tell the agent what you want to explore…"
                autoFocus
              />
              <ComposerPrimitive.Send
                className="studio-send"
                aria-label="Send message"
                disabled={consent.required && !consent.granted}
                title={consent.required && !consent.granted ? "Allow the selected provider above to start" : undefined}
              >
                <span aria-hidden="true">↗</span>
              </ComposerPrimitive.Send>
            </ComposerPrimitive.Root>
          </div>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>
  );
}

export function MoreActionsMenu({
  selected,
  busy,
  onSettings,
  onDelete,
}: {
  selected: Conversation | null;
  busy: boolean;
  onSettings: () => void;
  onDelete: (conversation: Conversation) => void;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus();
    const onPointer = (event: PointerEvent) => {
      if (
        !menuRef.current?.contains(event.target as Node)
        && !triggerRef.current?.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open ]);

  const moveFocus = (delta: number) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("button:not([disabled])") ?? []);
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLButtonElement);
    items[(at + delta + items.length) % items.length].focus();
  };

  const act = (fn: () => void) => () => {
    setOpen(false);
    triggerRef.current?.focus();
    fn();
  };

  return (
    <div className="studio-more-wrap">
      <button
        ref={triggerRef}
        type="button"
        className="studio-more-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls="studio-more-menu"
        onClick={() => setOpen((current) => !current)}
      aria-label="More actions" title="More actions"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="12" cy="19" r="1.8" /></svg></button>
      {open && (
        <div
          ref={menuRef}
          id="studio-more-menu"
          className="studio-more-menu ui-dropdown-menu"
          role="menu"
          aria-label="Conversation actions"
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") { event.preventDefault(); moveFocus(1); }
            else if (event.key === "ArrowUp") { event.preventDefault(); moveFocus(-1); }
            else if (event.key === "Home") { event.preventDefault(); menuRef.current?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus(); }
            else if (event.key === "End") { event.preventDefault(); const items = menuRef.current?.querySelectorAll<HTMLButtonElement>("button:not([disabled])"); items?.[items.length - 1]?.focus(); }
          }}
        >
          <button type="button" className="ui-dropdown-option" role="menuitem" onClick={act(onSettings)}>System Prompt</button>
          <button
            type="button"
            role="menuitem"
            className="is-danger ui-dropdown-option"
            disabled={!selected || busy}
            onClick={act(() => { if (selected) onDelete(selected); })}
          >Delete conversation…</button>
        </div>
      )}
    </div>
  );
}

type ReferenceChannel = Channel & { summary: string; selected: boolean; needs_renewal: boolean };

export function MyChannels({ channels, selectedChannelId, conversationId, busy = false }: {
  channels: Channel[]; selectedChannelId: number | null; conversationId: string | null; busy?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [references, setReferences] = useState<ReferenceChannel[]>([]);
  const [permissions, setPermissions] = useState<Set<number>>(new Set());
  const [pending, setPending] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [runBusy, setRunBusy] = useState(busy);
  // A stream can leave its local busy hint behind after completion. Confirm
  // it against this conversation, while the server still guards every update.
  useEffect(() => {
    if (!busy) { setRunBusy(false); return; }
    if (!open || !conversationId) { setRunBusy(busy); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const reconcile = async () => {
      try {
        const payload = await api<{ run: RunSummary | null }>(`/studio/api/conversations/${conversationId}/active-run`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        const active = isActiveRun(payload.run);
        setRunBusy(active);
        if (active) timer = setTimeout(() => { void reconcile(); }, 1500);
      } catch {
        // Let the update endpoint give a precise error instead of blocking
        // the control forever because a status request failed.
        if (!controller.signal.aborted) setRunBusy(false);
      }
    };
    void reconcile();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [busy, open, conversationId]);
  const sectionRef = useRef<HTMLElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    setReferences([]); setPermissions(new Set()); setError("");
    if (!conversationId) return;
    const controller = new AbortController();
    void api<{ references: ReferenceChannel[] }>(`/studio/api/conversations/${conversationId}/references`, { signal: controller.signal })
      .then((payload) => setReferences(payload.references))
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load reference channels."); });
    return () => controller.abort();
  }, [conversationId]);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { if (!sectionRef.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setOpen(false); triggerRef.current?.focus(); } };
    document.addEventListener("pointerdown", dismiss); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); };
  }, [open]);
  const update = async (channel: ReferenceChannel) => {
    if (!conversationId || pending !== null) return;
    setPending(channel.id); setError("");
    try {
      const payload = await api<{references: ReferenceChannel[]}>(`/studio/api/conversations/${conversationId}/references/${channel.id}`, {
        method:"PUT", headers:{"content-type":"application/json", "x-csrf-token":csrfToken()},
        body:JSON.stringify({enabled:!channel.selected, permission:permissions.has(channel.id)}),
      });
      setReferences(payload.references);
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "Could not update the reference."); }
    finally { setPending(null); }
  };
  const others = channels.filter((channel) => channel.id !== selectedChannelId);
  const selectedCount = references.filter((channel) => channel.selected).length;
  if (others.length === 0) return null;
  return <section ref={sectionRef} className="studio-my-channels" aria-label="References">
    <button ref={triggerRef} type="button" className="studio-my-channels-toggle ui-dropdown-trigger" aria-expanded={open} aria-controls="studio-my-channels-body" onClick={() => setOpen((current) => !current)}>
      <span className="studio-my-channels-copy"><strong>References{selectedCount ? ` · ${selectedCount}` : ""}</strong></span><span className="ui-chevron" aria-hidden="true" />
    </button>
    {open && <div id="studio-my-channels-body" className="studio-my-channels-body ui-dropdown-menu ui-dropdown-content">
      <div className="studio-reference-heading"><h2>Reference channels</h2><button type="button" aria-label="Close reference channels" onClick={() => { setOpen(false); triggerRef.current?.focus(); }}><i className="mgc mgc-close-core-regular" aria-hidden="true" /></button></div>
      <p>Use your other channels as references for this conversation.</p>
      {runBusy && <p className="studio-reference-wait" role="status">Wait for the current reply to finish before changing its sources.</p>}
      {!conversationId && <p>Create a conversation to add a reference.</p>}
      {error && <p role="alert">{error}</p>}
      <ul>{references.map((channel) => <li key={channel.id} className="studio-my-channels-card">
        <div><strong>{channelLabel(channel)}</strong><span>{channel.identifier}</span></div>
        <p className="studio-reference-summary">{channel.summary || "No profile has been built for this channel yet."}</p>
        {!channel.selected && <label className="studio-reference-permission"><input type="checkbox" checked={permissions.has(channel.id)} onChange={(event) => { const checked = event.currentTarget.checked; setPermissions((current) => { const next = new Set(current); if (checked) next.add(channel.id); else next.delete(channel.id); return next; }); }} />I control this channel and permit sharing its posts with the configured AI provider.</label>}
        <button type="button" disabled={runBusy || pending !== null || (!channel.selected && !permissions.has(channel.id))} onClick={() => { void update(channel); }}>{pending === channel.id ? "Connecting…" : channel.selected ? "Remove from context" : channel.needs_renewal ? "Renew reference access" : "Use in this conversation"}</button>
        {channel.selected && <span className="studio-reference-selected" role="status">Connected to this conversation</span>}
      </li>)}</ul>
    </div>}
  </section>;
}

export function ConversationTitle({ conversation, onRenamed }: { conversation: Conversation | null; onRenamed: (conversation: Conversation) => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const cancelled = useRef(false);
  const savingRef = useRef(false);
  const mounted = useRef(true);
  const scope = useRef(conversation?.id);
  useLayoutEffect(() => { scope.current = conversation?.id; }, [conversation?.id]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { setEditing(false); setError(""); }, [conversation?.id]);
  useEffect(() => { if (editing) { inputRef.current?.focus(); inputRef.current?.select(); } }, [editing]);
  const save = async () => {
    if (!conversation || cancelled.current || savingRef.current) return;
    const title = value.trim();
    if (!title || title === conversation.title) { setEditing(false); return; }
    savingRef.current = true; setSaving(true); setError("");
    try {
      const result = await api<{conversation:Conversation}>(`/studio/api/conversations/${conversation.id}`, {method:"PATCH", headers:{"content-type":"application/json","x-csrf-token":csrfToken()}, body:JSON.stringify({title})});
      if (mounted.current && scope.current === conversation.id) { onRenamed(result.conversation); setEditing(false); }
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "Could not rename the conversation."); }
    finally { savingRef.current=false; setSaving(false); }
  };
  return <div className="studio-title-row">
    {editing ? <input ref={inputRef} className="studio-title-input" aria-label="Conversation title" maxLength={160} value={value} disabled={saving} onChange={(event) => setValue(event.target.value)} onBlur={() => { void save(); }} onKeyDown={(event) => {
      if (event.key === "Enter") { event.preventDefault(); void save(); }
      if (event.key === "Escape") { event.preventDefault(); cancelled.current = true; setEditing(false); setError(""); }
    }} /> : <h1>{conversation ? <button className="studio-title-trigger" type="button" aria-label="Rename conversation" title={`${conversation.title} · Click to rename`} onClick={() => { cancelled.current=false; setValue(conversation.title); setEditing(true); }}>{conversation.title}</button> : "Content Studio"}</h1>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

function StudioApp() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [selectedChannelId, setSelectedChannelId] = useState<number | null>(() => {
    const value = Number(new URLSearchParams(window.location.search).get("channel"));
    return Number.isInteger(value) && value > 0 ? value : null;
  });
  const [selected, setSelected] = useState<Conversation | null>(null);
  const [error, setError] = useState("");
  const [inlineError, setInlineError] = useState("");
  const [deleteRetryConversation, setDeleteRetryConversation] = useState<Conversation | null>(null);
  const [draftOpen, setDraftOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [agentRunActive, setAgentRunActive] = useState(false);
  const [draftRefreshToken, setDraftRefreshToken] = useState(0);
  const bootstrapRef = useRef<Bootstrap | null>(null);
  const selectedChannelRef = useRef<number | null>(selectedChannelId);
  const refreshSequence = useRef(0);
  const refreshAbort = useRef<AbortController | null>(null);
  const chatScroll = useRef(0);
  const [pendingPrefill, setPendingPrefill] = useState<StudioPrefill | null>(null);

  useEffect(() => {
    bootstrapRef.current = bootstrap;
  }, [bootstrap]);

  const showRequestError = useCallback((reason: unknown, fallback: string) => {
    const message = reason instanceof Error && reason.message.trim() ? reason.message : fallback;
    if (bootstrapRef.current) {
      setInlineError(message);
      setError("");
    } else {
      setError(message);
    }
  }, []);

  const refresh = useCallback((channelOverride?: number | null) => {
    const channelId = channelOverride === undefined ? selectedChannelRef.current : channelOverride;
    const requestSequence = ++refreshSequence.current;
    refreshAbort.current?.abort();
    const controller = new AbortController();
    refreshAbort.current = controller;
    const url = channelId === null
      ? "/studio/api/bootstrap"
      : `/studio/api/bootstrap?channel_id=${encodeURIComponent(channelId)}`;
    void api<Bootstrap>(url, { signal: controller.signal })
      .then((payload) => {
        if (requestSequence !== refreshSequence.current) return;
        // A delayed bootstrap for a deselected channel must not overwrite
        // the freshly selected desk.
        if (channelId !== null && channelId !== selectedChannelRef.current) return;
        setBootstrap(payload);
        bootstrapRef.current = payload;
        setSelectedChannelId(payload.selected_channel_id);
        selectedChannelRef.current = payload.selected_channel_id;
        setInlineError("");
        setSelected((current) =>
          payload.conversations.find((conversation) => conversation.id === current?.id) ??
            payload.current_conversation,
        );
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        if (requestSequence === refreshSequence.current) {
          showRequestError(reason, "Studio could not load.");
        }
      });
  }, [showRequestError]);

  const handleRunFinished = useCallback(() => {
    setAgentRunActive(false);
    setDraftRefreshToken((current) => current + 1);
    refresh();
  }, [refresh]);

  useEffect(() => {
    setAgentRunActive(false);
  }, [selected?.id]);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (agentRunActive) {
      const timer = window.setTimeout(refresh, 1500);
      return () => window.clearTimeout(timer);
    }
  }, [agentRunActive, refresh]);

  const cancelRun = useCallback(async (runId: string) => {
    await api(`/studio/api/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      headers: { "x-csrf-token": csrfToken() },
    });
  }, []);


  const selectChannel = useCallback((channelId: number) => {
    if (!Number.isInteger(channelId) || channelId <= 0 || channelId === selectedChannelRef.current) return;
    if (draftOpen && !window.confirm("Switch channels and close the current draft workspace? Unsaved draft edits will be discarded.")) return;
    selectedChannelRef.current = channelId;
    setSelectedChannelId(channelId);
    setSelected(null);
    setDraftOpen(false);
    setSettingsOpen(false);
    setProfileOpen(false);
    setAgentRunActive(false);
    setInlineError("");
    setBootstrap((current) => current ? {
      ...current,
      selected_channel_id: channelId,
      conversations: [],
      current_conversation: null,
      draft: null,
      active_run: null,
      profile: null,
      profile_status: "not_built",
    } : current);
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("channel", String(channelId));
    window.history.replaceState({}, "", nextUrl);
    refresh(channelId);
  }, [draftOpen, refresh]);

  const createConversation = useCallback(() => {
    const channelId = selectedChannelRef.current;
    if (!channelId) return;
    setInlineError("");
    void api<{ conversation: Conversation }>("/studio/api/conversations", {
      method: "POST",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken() },
      body: JSON.stringify({ channel_id: channelId }),
    })
      .then((payload) => {
        // Ignore the late creation if the channel moved on meanwhile.
        if (selectedChannelRef.current !== channelId) return;
        setSelected(payload.conversation);
        refresh();
      })
      .catch((reason: unknown) => showRequestError(reason, "Could not create conversation."));
  }, [refresh, showRequestError]);

  const applyDeletedConversation = useCallback((conversation: Conversation) => {
    setSelected((current) => current?.id === conversation.id ? null : current);
    setBootstrap((current) => {
      if (!current) return current;
      const conversations = current.conversations.filter((item) => item.id !== conversation.id);
      const removedCurrent = current.current_conversation?.id === conversation.id;
      const next = {
        ...current,
        conversations,
        current_conversation: removedCurrent ? (conversations[0] ?? null) : current.current_conversation,
        draft: removedCurrent ? null : current.draft,
        active_run: removedCurrent ? null : current.active_run,
      };
      bootstrapRef.current = next;
      return next;
    });
    refresh();
  }, [refresh]);

  const deleteRequest = useCallback(async (conversation: Conversation) => {
    setDeletingId(conversation.id);
    setInlineError("");
    setDeleteRetryConversation(null);
    try {
      await api<{ conversation_id: string; deleted: boolean }>(`/studio/api/conversations/${encodeURIComponent(conversation.id)}`, {
        method: "DELETE",
        headers: { "x-csrf-token": csrfToken() },
      });
      applyDeletedConversation(conversation);
    } catch (reason: unknown) {
      if (isConversationActiveRunError(reason)) {
        setInlineError(reason instanceof StudioApiError ? reason.message : "Stop the active run before deleting this conversation.");
        setDeleteRetryConversation(conversation);
      } else {
        showRequestError(reason, "Could not delete conversation.");
      }
    } finally {
      setDeletingId(null);
    }
  }, [applyDeletedConversation, showRequestError]);

  const deleteConversation = (conversation: Conversation) => {
    if (!window.confirm(`Delete “${conversation.title}” permanently? Its Studio messages, drafts, research, and run history will be removed. Telegram channel data will not be affected.`)) return;
    void deleteRequest(conversation);
  };

  // Explorer handoff contract (see api.ts): a same-tab prefill event or a
  // stored cross-page reference selects the post's own channel and prefills
  // the composer. Nothing is ever sent automatically.
  useEffect(() => {
    const stored = consumeStoredPrefill();
    if (stored) setPendingPrefill(stored);
    const onPrefill = (event: Event) => {
      const prefill = parsePrefillDetail((event as CustomEvent<unknown>).detail);
      if (prefill) setPendingPrefill(prefill);
    };
    window.addEventListener(PREFILL_EVENT, onPrefill);
    return () => window.removeEventListener(PREFILL_EVENT, onPrefill);
  }, []);

  // Route a pending prefill to its own channel. Channel switches keep their
  // unsaved-draft guard; if the target channel has no conversation yet, one
  // empty conversation is opened so the composer exists. Nothing is sent.
  useEffect(() => {
    if (!pendingPrefill || !bootstrap) return;
    if (pendingPrefill.channel_id !== selectedChannelId) {
      selectChannel(pendingPrefill.channel_id);
      return;
    }
    if (!selected && bootstrap.conversations.length === 0) {
      createConversation();
    }
  }, [pendingPrefill, bootstrap, selectedChannelId, selected, selectChannel, createConversation]);

  const handlePrefillResult = useCallback((_applied: boolean, _prefill: StudioPrefill) => {
    setPendingPrefill(null);
  }, []);

  const draftOpener = useRef<HTMLElement | null>(null);
  const openDraft = () => {
    draftOpener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const viewport = document.querySelector(".studio-viewport");
    chatScroll.current = viewport instanceof HTMLElement ? viewport.scrollTop : 0;
    setDraftOpen(true);
  };
  const closeDraft = useCallback(() => setDraftOpen(false), []);

  useEffect(() => {
    document.body.classList.toggle("studio-draft-open", draftOpen);
    const overlay = window.matchMedia("(max-width: 1080px)");
    const background = [...document.querySelectorAll<HTMLElement>(".studio-main, .studio-rail")];
    const updateIsolation = () => {
      background.forEach(node => { node.inert = draftOpen && overlay.matches; });
      if (draftOpen && overlay.matches) document.querySelector<HTMLElement>(".studio-draft-close")?.focus();
    };
    updateIsolation();
    overlay.addEventListener("change", updateIsolation);
    const escape = (event: KeyboardEvent) => {
      if (draftOpen && overlay.matches && event.key === "Escape") setDraftOpen(false);
    };
    document.addEventListener("keydown", escape);
    if (!draftOpen) {
      const top = chatScroll.current;
      const restore = () => {
        const viewport = document.querySelector(".studio-viewport");
        if (viewport instanceof HTMLElement) viewport.scrollTop = top;
        if (draftOpener.current?.isConnected) draftOpener.current.focus();
      };
      // The overlay unmounts first; restore on the next frame so Back
      // returns to the same chat scroll.
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(restore);
      else restore();
    }
    return () => {
      document.body.classList.remove("studio-draft-open");
      background.forEach(node => { node.inert = false; });
      overlay.removeEventListener("change", updateIsolation);
      document.removeEventListener("keydown", escape);
    };
  }, [draftOpen]);

  const waitForRunToFinish = useCallback(async (conversationId: string, runId: string) => {
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      const payload = await api<{ run: RunSummary | null }>(`/studio/api/conversations/${encodeURIComponent(conversationId)}/active-run`);
      if (!payload.run || payload.run.id !== runId || !isActiveRun(payload.run)) return;
      await new Promise<void>((resolve) => window.setTimeout(resolve, 500));
    }
    throw new Error("The run is still active. Try deleting the conversation again when it finishes.");
  }, []);

  const stopAndDelete = useCallback(async () => {
    const conversation = deleteRetryConversation;
    if (!conversation) return;
    setDeleteRetryConversation(null);
    setInlineError("Stopping the active run…");
    setDeletingId(conversation.id);
    try {
      const payload = await api<{ run: RunSummary | null }>(`/studio/api/conversations/${encodeURIComponent(conversation.id)}/active-run`);
      if (payload.run && isActiveRun(payload.run)) {
        await cancelRun(payload.run.id);
        await waitForRunToFinish(conversation.id, payload.run.id);
      }
      await api<{ conversation_id: string; deleted: boolean }>(`/studio/api/conversations/${encodeURIComponent(conversation.id)}`, {
        method: "DELETE",
        headers: { "x-csrf-token": csrfToken() },
      });
      applyDeletedConversation(conversation);
    } catch (reason: unknown) {
      setDeleteRetryConversation(conversation);
      showRequestError(reason, "Could not stop the run and delete conversation.");
    } finally {
      setDeletingId(null);
    }
  }, [applyDeletedConversation, cancelRun, deleteRetryConversation, showRequestError, waitForRunToFinish]);

  if (error && !bootstrap) return <div className="studio-error" role="alert"><strong>Studio could not load.</strong><span>{error}</span><button type="button" onClick={() => { setError(""); refresh(); }}>Try again</button></div>;
  if (!bootstrap) return <div className="studio-loading studio-page-loading" role="status">Opening your workspace…</div>;
  if (!bootstrap.setup.ready) return null;
  const selectedChannel = bootstrap.channels.find((channel) => channel.id === selectedChannelId) ?? null;
  const selectedChannelLabel = selectedChannel?.title?.trim() || selectedChannel?.identifier || "this channel";

  return (
    <div className="studio-app">
      <ConversationRail channels={bootstrap.channels} selectedChannelId={selectedChannelId} onChannelSelect={selectChannel} conversations={bootstrap.conversations} selected={selected} onSelect={setSelected} onNew={createConversation} />
      {settingsOpen && selectedChannelId && <StudioSettings channelId={selectedChannelId} channelLabel={selectedChannelLabel} onClose={() => setSettingsOpen(false)} />}
      {profileOpen && selectedChannelId && (
        <ChannelProfileDialog
          channelId={selectedChannelId}
          onClose={() => setProfileOpen(false)}
          onSaved={(profile) => {
            setBootstrap((current) => current ? { ...current, profile, profile_status: (profile.topics_text?.trim() || profile.editorial_text?.trim() || profile.style_text?.trim()) ? "ready" : "not_built" } : current);
          }}
        />
      )}
      <main className="studio-main">
        <header className="studio-topbar">
          <div>
            <p className="studio-overline">{selectedChannel?.identifier ?? "Channel"}</p>
            <ConversationTitle key={selected?.id} conversation={selected} onRenamed={(conversation) => { setSelected(conversation); refresh(); }} />
            <ProfilePrimer bootstrap={bootstrap} onBootstrap={(next) => setBootstrap(next)} />
          </div>
          <div className="studio-topbar-actions">
            <button type="button" className="studio-draft-toggle" onClick={openDraft}>Draft</button>
            <MyChannels key={selected?.id} channels={bootstrap.channels} selectedChannelId={selectedChannelId} conversationId={selected?.id ?? null} busy={agentRunActive} />
            <button type="button" className="studio-profile-trigger" aria-haspopup="dialog" aria-expanded={profileOpen} title="View and edit channel profile" onClick={() => setProfileOpen(true)}>Profile</button>
            <MoreActionsMenu
              selected={selected}
              busy={deletingId !== null}
              onSettings={() => setSettingsOpen(true)}
              onDelete={deleteConversation}
            />
          </div>
        </header>
        {(inlineError || (error && bootstrap)) && <div className="studio-inline-error" role="alert"><span>{inlineError || error}</span>{deleteRetryConversation && <button type="button" onClick={() => void stopAndDelete()} disabled={deletingId !== null}>{deletingId === deleteRetryConversation.id ? "Stopping…" : "Stop run and delete"}</button>}<button type="button" className="studio-inline-error-dismiss" onClick={() => { setInlineError(""); setError(""); setDeleteRetryConversation(null); }} aria-label="Dismiss error">×</button></div>}
        {selected ? <StudioThread key={selected.id} conversation={selected} seedRun={selected.id === bootstrap.current_conversation?.id ? bootstrap.active_run : null} consent={bootstrap.consent} pendingPrefill={pendingPrefill && pendingPrefill.channel_id === selected.channel_id ? pendingPrefill : null} onPrefillResult={handlePrefillResult} onStopRun={cancelRun} onRunActivityChange={setAgentRunActive} onRunFinished={handleRunFinished} /> : <div className="studio-no-thread"><h2>Start a conversation</h2><p>Choose New conversation to give the agent a channel context.</p><button type="button" onClick={createConversation}>Open channel desk</button></div>}
      </main>
      <DraftPanel
        conversationId={selected?.id ?? null}
        seedDraft={bootstrap.draft}
        open={draftOpen}
        onClose={closeDraft}
        onSelectConversation={(conversationId) => {
          const previous = bootstrap.conversations.find((item) => item.id === conversationId);
          if (previous) setSelected(previous);
        }}
        watchForAgentChanges={agentRunActive}
        refreshToken={draftRefreshToken}
      />
    </div>
  );
}

const mount = document.getElementById("studio-react-root");
if (mount) createRoot(mount).render(<StudioApp />);
const calendarMount = document.getElementById("calendar-react-root");
if (calendarMount) createRoot(calendarMount).render(<CalendarPage />);
