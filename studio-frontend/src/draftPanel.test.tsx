import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StudioApiError } from "./api";
import { DraftPanel, draftCounterState, draftSaveErrorMessage } from "./main";
import type { Draft, DraftVersion } from "./api";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("DraftPanel state helpers", () => {
  it("uses the displayed version for the counter and copy limit", () => {
    expect(draftCounterState("short", 100)).toEqual({ count: 100, overLimit: false, warning: false });
    expect(draftCounterState("short", 5000)).toEqual({ count: 5000, overLimit: true, warning: true });
  });

  it("surfaces the server's validation message", () => {
    const error = new StudioApiError(422, { error: { message: "Headings are not allowed in the draft body." } });
    expect(draftSaveErrorMessage(error)).toBe("Headings are not allowed in the draft body.");
  });
});

it("saves links as a new version and reads the older title/body/links without saving", async () => {
  let draft = { id: "draft", conversation_id: "chat", channel_id: 1, working_title: "Original",
    body: "Original body", source_links: ["https://example.test/a"], media_ids: [],
    revision: 1, current_version: 1, character_count: 13 } as unknown as Draft;
  const versions: DraftVersion[] = [{ id: 1, draft_id: "draft", version: 1,
    working_title: "Original", body: "Original body", source_links: ["https://example.test/a"], media_ids: [],
    origin: "generated", instruction: "", character_count: 13, created_at: null }];
  const saves: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    let data;
    if (init?.method === "PATCH") {
      const payload = JSON.parse(String(init.body)); saves.push(payload);
      draft = { ...draft, ...payload, revision: 2, current_version: 2 };
      versions.push({ ...versions[0], id: 2, version: 2, working_title: draft.working_title, source_links: draft.source_links });
      data = { draft };
    } else if (url.includes("/versions")) data = { versions };
    else if (url.includes("/media")) data = { media: [] };
    else data = { draft };
    return new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } });
  }));
  render(<DraftPanel conversationId="chat" seedDraft={draft} open onClose={() => {}}
    onSelectConversation={() => {}} watchForAgentChanges={false} refreshToken={0} />);
  await waitFor(() => expect(screen.getByLabelText("Draft version").querySelectorAll("option")).toHaveLength(1));
  fireEvent.change(screen.getByLabelText("Artifact title"), { target: { value: "Updated" } });
  fireEvent.change(screen.getByLabelText("Source links"), { target: { value: "https://example.test/b" } });
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(screen.getByLabelText("Draft version").querySelectorAll("option")).toHaveLength(2));
  expect(saves).toEqual([expect.objectContaining({ expected_revision: 1, working_title: "Updated",
    source_links: ["https://example.test/b"], save_as_new_version: true })]);
  fireEvent.change(screen.getByLabelText("Draft version"), { target: { value: "1" } });
  expect((screen.getByLabelText("Artifact title") as HTMLInputElement).value).toBe("Original");
  expect((screen.getByLabelText("Source links") as HTMLTextAreaElement).value).toBe("https://example.test/a");
  expect(screen.getByText("Original body")).toBeTruthy();
  expect((screen.getByLabelText("Source links") as HTMLTextAreaElement).readOnly).toBe(true);
  expect(saves).toHaveLength(1);
});

it("lets the owner type directly, save a formatted version, and keep typing during Save", async () => {
  let draft = { id: "draft", conversation_id: "chat", channel_id: 1, working_title: "Original",
    body: "**Headline**\nOriginal body", body_html: "<b>Headline</b><br>Original body", source_links: [], media_ids: [],
    revision: 1, current_version: 1, character_count: 22 } as unknown as Draft;
  const versions = [{ ...draft, version: 1, origin: "generated" }] as unknown as DraftVersion[];
  const saves: Record<string, unknown>[] = [];
  let resolveSave!: () => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    let data;
    if (init?.method === "PATCH") {
      const payload = JSON.parse(String(init.body)); saves.push(payload);
      if (saves.length === 1) await new Promise<void>(resolve => { resolveSave = resolve; });
      draft = { ...draft, ...payload, body_html: undefined, revision: draft.revision + 1, current_version: draft.current_version + 1 };
      versions.push({ ...draft, version: draft.current_version, origin: "user" } as unknown as DraftVersion);
      data = { draft };
    } else if (url.includes("/versions")) data = { versions };
    else if (url.includes("/media")) data = { media: [] };
    else data = { draft };
    return new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } });
  }));
  const props = { conversationId: "chat", seedDraft: draft, open: true, onClose: () => {},
    onSelectConversation: () => {}, watchForAgentChanges: false, refreshToken: 0 };
  const { unmount } = render(<DraftPanel {...props} />);
  await waitFor(() => expect(screen.getByLabelText("Draft version").querySelectorAll("option")).toHaveLength(1));
  expect(screen.queryByRole("button", { name: "Edit post" })).toBeNull();
  const editor = screen.getByRole("textbox", { name: "Full post — headline and body" });
  expect(editor.getAttribute("contenteditable")).toBe("true");
  editor.innerHTML = "<b>Headline</b><br>First edit";
  fireEvent.input(editor);
  expect(saves).toHaveLength(0);
  const save = screen.getByRole("button", { name: /^Save$/ }) as HTMLButtonElement;
  expect(save.closest(".studio-panel-heading")).not.toBeNull();
  fireEvent.click(save);
  await waitFor(() => expect(saves).toHaveLength(1));
  editor.innerHTML = "<b>Headline</b><br>Continued typing";
  fireEvent.input(editor);
  expect(save.disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Schedule" }) as HTMLButtonElement).disabled).toBe(true);
  resolveSave();
  await waitFor(() => expect(save.disabled).toBe(false));
  expect(editor.textContent).toContain("Continued typing");
  expect(editor.getAttribute("contenteditable")).toBe("true");
  fireEvent.click(save);
  await waitFor(() => expect(screen.getByLabelText("Draft version").querySelectorAll("option")).toHaveLength(3));
  expect(saves).toEqual([
    expect.objectContaining({ body: "**Headline**\nFirst edit", expected_revision: 1, save_as_new_version: true }),
    expect.objectContaining({ body: "**Headline**\nContinued typing", expected_revision: 2, save_as_new_version: true }),
  ]);
  unmount();
  const refreshed = render(<DraftPanel {...props} seedDraft={draft} />);
  await waitFor(() => expect(screen.getByLabelText("Draft version").querySelectorAll("option")).toHaveLength(3));
  expect(screen.getByRole("textbox", { name: "Full post — headline and body" }).querySelector("b")!.textContent).toBe("Headline");
  expect(screen.getByRole("textbox", { name: "Full post — headline and body" }).textContent).toContain("Continued typing");
  fireEvent.change(screen.getByLabelText("Draft version"), { target: { value: "1" } });
  expect(screen.getByRole("textbox", { name: "Full post — headline and body" }).getAttribute("contenteditable")).toBe("false");
  expect(saves).toHaveLength(2);
  fireEvent.change(screen.getByLabelText("Draft version"), { target: { value: "3" } });
  const current = screen.getByRole("textbox", { name: "Full post — headline and body" });
  current.textContent = "Local edit while the agent continues";
  fireEvent.input(current);
  draft = { ...draft, body: "New agent version", revision: 4, current_version: 4 };
  versions.push({ ...draft, version: 4, origin: "regenerated" } as unknown as DraftVersion);
  refreshed.rerender(<DraftPanel {...props} seedDraft={draft} refreshToken={1} />);
  await waitFor(() => expect(screen.getByLabelText("Draft version").querySelectorAll("option")).toHaveLength(4));
  expect(current.textContent).toBe("Local edit while the agent continues");
  expect(current.getAttribute("contenteditable")).toBe("true");
  expect(saves).toHaveLength(2);
});

it("keeps the latest typing when a save conflicts and retries with the current revision", async () => {
  const original = { id: "draft", conversation_id: "chat", channel_id: 1, working_title: "Original",
    body: "Original body", source_links: [], media_ids: [], revision: 1, current_version: 1 } as unknown as Draft;
  const server = { ...original, body: "Server body", revision: 2, current_version: 2 };
  const versions = [{ ...original, version: 1 }, { ...server, version: 2 }] as unknown as DraftVersion[];
  const saves: Record<string, unknown>[] = [];
  let rejectSave!: () => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    let data;
    if (init?.method === "PATCH") {
      const payload = JSON.parse(String(init.body)); saves.push(payload);
      if (saves.length === 1) {
        await new Promise<void>(resolve => { rejectSave = resolve; });
        return new Response(JSON.stringify({ server_draft: server }), { status: 409, headers: { "content-type": "application/json" } });
      }
      data = { draft: { ...server, ...payload, revision: 3, current_version: 3 } };
    } else if (url.includes("/versions")) data = { versions };
    else if (url.includes("/media")) data = { media: [] };
    else data = { draft: original };
    return new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } });
  }));
  render(<DraftPanel conversationId="chat" seedDraft={original} open onClose={() => {}}
    onSelectConversation={() => {}} watchForAgentChanges={false} refreshToken={0} />);
  await waitFor(() => expect(screen.getByLabelText("Draft version").querySelectorAll("option")).toHaveLength(2));
  const editor = screen.getByRole("textbox", { name: "Full post — headline and body" });
  editor.textContent = "First local edit"; fireEvent.input(editor);
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(saves).toHaveLength(1));
  editor.textContent = "Latest typing"; fireEvent.input(editor);
  rejectSave();
  fireEvent.click(await screen.findByRole("button", { name: "Keep my text" }));
  expect(editor.textContent).toBe("Latest typing");
  expect(editor.getAttribute("contenteditable")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
  await waitFor(() => expect(saves).toHaveLength(2));
  expect(saves[1]).toEqual(expect.objectContaining({ body: "Latest typing", expected_revision: 2 }));
});
