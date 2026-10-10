import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { DraftBodyEditor, markdownFromEditable } from "./DraftBodyEditor";
import { htmlFromMarkdown } from "./markdownCopy";

afterEach(cleanup);
const parse = (html: string) => {
  const host = document.createElement("template");
  host.innerHTML = html;
  return markdownFromEditable(host.content);
};

describe("direct formatted draft editing", () => {
  it("preserves the supported formatting, links, quotes and blank lines", () => {
    const body = "**Headline**\n\nBody *italic* ~~strike~~ `code` [source](https://example.test/a_(b)?x=1&y=2)\n> quote\n> second";
    expect(parse(htmlFromMarkdown(body))).toBe(body);
  });
  it.each([
    ["first<div>second</div><div><br></div><div>last</div>", "first\nsecond\n\nlast"],
    ["<p>first</p><p><br></p><p>last</p>", "first\n\nlast"],
    ["<br>", "\n"],
    ["<div><br></div>", ""],
    ["<b>first<br>second</b>", "**first**\n**second**"],
  ])("keeps browser-generated paragraphs and blank lines: %s", (html, body) => {
    expect(parse(html)).toBe(body);
  });
  it("edits immediately and preserves the DOM/selection when the parent updates", () => {
    const onChange = vi.fn();
    const props = { readOnly: false, onChange };
    const { rerender } = render(<DraftBodyEditor {...props} body="**Headline**" html="<b>Headline</b>" />);
    const editor = screen.getByRole("textbox");
    expect(editor.getAttribute("contenteditable")).toBe("true");
    const text = editor.querySelector("b")!.firstChild!;
    text.textContent = "Edited headline";
    const range = document.createRange(); range.setStart(text, 6); range.collapse(true);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
    fireEvent.input(editor);
    expect(onChange).toHaveBeenLastCalledWith("**Edited headline**");
    rerender(<DraftBodyEditor {...props} body="**Edited headline**" html="<b>Edited headline</b>" />);
    expect(editor.querySelector("b")!.firstChild).toBe(text);
    expect(window.getSelection()!.anchorOffset).toBe(6);
    rerender(<DraftBodyEditor {...props} body="Server revision" html="Server revision" />);
    expect(editor.textContent).toBe("Server revision");
  });
  it("sanitizes rich paste while retaining supported formatting and line breaks", () => {
    const onChange = vi.fn();
    render(<DraftBodyEditor body="Start " html="Start " readOnly={false} onChange={onChange} />);
    const editor = screen.getByRole("textbox");
    const range = document.createRange(); range.selectNodeContents(editor); range.collapse(false);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
    fireEvent.paste(editor, { clipboardData: { getData: (type: string) => type === "text/html"
      ? '<b onclick="bad()">safe</b><br><a href="javascript:bad()">label</a><script>bad()</script><img src="https://example.test/tracker">' : "safe" } });
    expect(onChange).toHaveBeenLastCalledWith("Start **safe**\nlabel");
    expect(editor.querySelector("script, img, [onclick]")).toBeNull();
    expect(editor.querySelector("a")).toBeNull();
  });
  it("waits for IME composition to complete before committing an edit", () => {
    const onChange = vi.fn();
    render(<DraftBodyEditor body="" html="" readOnly={false} onChange={onChange} />);
    const editor = screen.getByRole("textbox");
    fireEvent.compositionStart(editor);
    editor.textContent = "中文";
    fireEvent.input(editor);
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.compositionEnd(editor);
    expect(onChange).toHaveBeenLastCalledWith("中文");
  });
  it("keeps historical versions read-only", () => {
    const onChange = vi.fn();
    render(<DraftBodyEditor body="Old" html="Old" readOnly onChange={onChange} />);
    const editor = screen.getByRole("textbox");
    expect(editor.getAttribute("contenteditable")).toBe("false");
    expect(editor.getAttribute("aria-readonly")).toBe("true");
    fireEvent.input(editor);
    expect(onChange).not.toHaveBeenCalled();
  });
});
