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
