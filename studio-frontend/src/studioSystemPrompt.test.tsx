import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StudioSettings } from "./main";

function stubSettingsFetch(prompt: string, onPatch?: (body: unknown) => unknown) {
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url);
    if (init?.method === "PATCH" && target.includes("/studio/api/settings")) {
      if (onPatch) return onPatch(JSON.parse(String(init.body))) as unknown as Response;
      return {
        ok: true,
        status: 200,
        redirected: false,
        headers: { get: () => "application/json" },
        json: async () => ({ channel_id: 7, system_prompt: prompt }),
      } as unknown as Response;
    }
    return {
      ok: true,
      status: 200,
      redirected: false,
      headers: { get: () => "application/json" },
      json: async () => ({ channel_id: 7, system_prompt: prompt }),
    } as unknown as Response;
  }));
}

describe("StudioSettings channel prompt", () => {
  beforeEach(() => {
    HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    });
  });

  it("stays channel-scoped with the 12,000-character bound", async () => {
    stubSettingsFetch("Be brief.");
    render(<StudioSettings channelId={7} channelLabel="@chan" onClose={() => {}} />);
    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    await waitFor(() => expect(box.value).toBe("Be brief."));
    expect(box.getAttribute("maxlength")).toBe("12000");
    expect(screen.getByText(/applies to this channel's conversations only|never apply to another channel/i)).toBeDefined();
  });

  it("guards unsaved edits on close and keeps text on save failure", async () => {
    stubSettingsFetch("Be brief.", () => {
      throw new Error("network down");
    });
    const onClose = vi.fn();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<StudioSettings channelId={7} channelLabel="@chan" onClose={onClose} />);
    const box = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "Be brief and kind." } });
    expect(screen.getByText(/Unsaved changes/)).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Close System Prompt" }));
    expect(confirm).toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    confirm.mockRestore();

    fireEvent.click(screen.getByRole("button", { name: "Save instructions" }));
    await screen.findByText(/Your text is preserved/);
    // Local edits survive the failed save.
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Be brief and kind.");
  });

  it("clears explicitly and saves the empty prompt", async () => {
    const seen: unknown[] = [];
    stubSettingsFetch("Be brief.", (body: unknown) => {
      seen.push(body);
      return {
        ok: true,
        status: 200,
        redirected: false,
        headers: { get: () => "application/json" },
        json: async () => ({ channel_id: 7, system_prompt: "" }),
      } as unknown as Response;
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<StudioSettings channelId={7} channelLabel="@chan" onClose={() => {}} />);
    await screen.findByRole("textbox");
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Save instructions" }));
    await waitFor(() => {
      expect(seen).toHaveLength(1);
    });
    expect(seen[0]).toMatchObject({ channel_id: 7, system_prompt: "" });
    confirm.mockRestore();
  });
});

it("sends the loaded prompt and preserves edits after a concurrent save", async () => {
  const bodies: unknown[] = [];
  stubSettingsFetch("Original", (body) => {
    bodies.push(body);
    return { ok: false, status: 409, redirected: false,
      headers: { get: () => "application/json" },
      json: async () => ({ error: { code: "system_prompt_conflict", message: "Changed in another tab" } }),
    } as unknown as Response;
  });
  render(<StudioSettings channelId={7} channelLabel="@chan" onClose={() => {}} />);
  const box = await screen.findByRole("textbox");
  await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe("Original"));
  fireEvent.change(box, { target: { value: "My edits" } });
  fireEvent.click(screen.getByRole("button", { name: "Save instructions" }));
  await screen.findByText(/changed in another tab/i);
  expect(bodies[0]).toMatchObject({ expected_prompt: "Original", system_prompt: "My edits" });
  expect((box as HTMLTextAreaElement).value).toBe("My edits");
});
