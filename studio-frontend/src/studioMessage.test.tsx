import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
const state = vi.hoisted(() => ({ message: { role: "assistant", content: [] as Array<{ type: string; text?: string }> }, html: "Response content" }));
vi.mock("@assistant-ui/react", async (importOriginal) => ({
  ...await importOriginal<object>(),
  useAuiState: (select: (value: typeof state) => unknown) => select(state),
  MessagePrimitive: {
    Root: ({ children, ...props }: React.PropsWithChildren<Record<string, unknown>>) => <div {...props}>{children}</div>,
    If: ({ children, ...roles }: React.PropsWithChildren<Record<string, unknown>>) => roles[state.message.role] ? <>{children}</> : null,
    Parts: () => <div dangerouslySetInnerHTML={{ __html: state.html }} />,
  },
}));
import { StudioMessage } from "./main";
let resizeCallback: (() => void) | undefined;
beforeEach(() => {
  state.html = "Response content";
  state.message = { role: "assistant", content: [{ type: "text", text: "Response content" }] };
  resizeCallback = undefined;
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resizeCallback = callback; }
    observe() {}
    disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe("assistant response bubbles", () => {
  it("mounts the bubble when a pending response starts streaming", () => {
    state.message = { role: "assistant", content: [] };
    const { container, rerender } = render(<StudioMessage />);
    expect(container.childElementCount).toBe(0);
    state.message = { role: "assistant", content: [{ type: "text", text: "First words" }] };
    rerender(<StudioMessage />);
    expect(container.querySelector('[data-role="assistant"]')).not.toBeNull();
  });
  it.each([[], [{ type: "text", text: "" }], [{ type: "text", text: " \n " }]].map(content => ({ content })))("omits the bubble until visible content arrives: %j", ({ content }) => {
    state.message = { role: "assistant", content };
    const { container } = render(<StudioMessage />);
    expect(container.childElementCount).toBe(0);
  });
  it.each([[{ type: "text", text: "Here is the answer." }], [{ type: "tool-call" }]].map(content => ({ content })))("shows actual text and tool activity: %j", ({ content }) => {
    state.message = { role: "assistant", content };
    render(<StudioMessage />);
    expect(screen.getAllByText("Response content").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Open message in wide view" })).toBeNull();
  });
  it("offers expansion for a rendered table even when it fits", () => {
    state.html = "<table><tbody><tr><td>Small table</td></tr></tbody></table>";
    render(<StudioMessage />);
    expect(screen.getByRole("button", { name: "Open message in wide view" })).not.toBeNull();
  });
  it("updates when streamed Markdown adds or removes a table", async () => {
    const { rerender } = render(<StudioMessage />);
    state.html = "<table><tbody><tr><td>Streamed table</td></tr></tbody></table>";
    rerender(<StudioMessage />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Open message in wide view" })).not.toBeNull());
    state.html = "Short text again";
    rerender(<StudioMessage />);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Open message in wide view" })).toBeNull());
  });
  it.each(["Width", "Height"])("detects nested content overflowing its %s and removes expansion when it fits again", async (axis) => {
    state.html = "<pre>Code that can overflow</pre>";
    const { container } = render(<StudioMessage />);
    const code = container.querySelector("pre")!;
    Object.defineProperty(code, `client${axis}`, { configurable: true, value: 200 });
    Object.defineProperty(code, `scroll${axis}`, { configurable: true, value: 450 });
    resizeCallback?.();
    await waitFor(() => expect(screen.getByRole("button", { name: "Open message in wide view" })).not.toBeNull());
    Object.defineProperty(code, `scroll${axis}`, { configurable: true, value: 200 });
    resizeCallback?.();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Open message in wide view" })).toBeNull());
  });
  it("compares message height with the chat viewport, independent of other messages", async () => {
    const { container } = render(<div className="studio-viewport"><div>Other message</div><StudioMessage /></div>);
    const viewport = container.querySelector(".studio-viewport")!;
    const bubble = container.querySelector(".studio-message")!;
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 400 });
    vi.spyOn(bubble, "getBoundingClientRect").mockReturnValue({ height: 600 } as DOMRect);
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Open message in wide view" })).not.toBeNull());
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 800 });
    resizeCallback?.();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Open message in wide view" })).toBeNull());
  });
  it.each(["user", "system"])("keeps %s messages without an expansion control", (role) => {
    state.message.role = role;
    state.html = "<table><tbody><tr><td>User content</td></tr></tbody></table>";
    render(<StudioMessage />);
    expect(screen.queryByRole("button", { name: "Open message in wide view" })).toBeNull();
  });
});
