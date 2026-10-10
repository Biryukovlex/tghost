import { useLayoutEffect, useRef } from "react";
import { htmlFromMarkdown } from "./markdownCopy";

const blocks = new Set(["DIV", "P", "BLOCKQUOTE", "LI", "PRE"]);

/** Keep the existing draft dialect when the browser edits its rendered DOM. */
export function markdownFromEditable(root: Node): string {
  const children = (node: Node): string => {
    const nodes = Array.from(node.childNodes);
    return nodes.map((child, index) => {
      const previous = nodes[index - 1];
      const block = child instanceof Element && blocks.has(child.tagName);
      const previousBlock = previous instanceof Element && blocks.has(previous.tagName);
      const breakBoundary = child instanceof HTMLBRElement || previous instanceof HTMLBRElement;
      return (index > 0 && (block || previousBlock) && !breakBoundary ? "\n" : "") + visit(child);
    }).join("");
  };
  const visit = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return (node.textContent || "").replace(/\u00a0/g, " ");
    if (!(node instanceof Element)) return "";
    const tag = node.tagName;
    if (["SCRIPT", "STYLE", "IMG", "IFRAME", "OBJECT"].includes(tag)) return "";
    if (tag === "BR") return "\n";
    // Browsers use a lone <br> as the placeholder for an empty paragraph.
    const text = blocks.has(tag) && node.childNodes.length === 1 && node.firstChild instanceof HTMLBRElement ? "" : children(node);
    if (!text) return "";
    const wrap = (value: string, marker: string) => value.split("\n").map(line => line ? `${marker}${line}${marker}` : "").join("\n");
    if (tag === "B" || tag === "STRONG") return wrap(text, "**");
    if (tag === "I" || tag === "EM") return wrap(text, "*");
    if (["S", "STRIKE", "DEL"].includes(tag)) return wrap(text, "~~");
    if (tag === "CODE") return wrap(node.textContent || "", "`");
    if (tag === "BLOCKQUOTE") return text.split("\n").map(line => `> ${line}`).join("\n");
    if (tag === "A") {
      const href = node.getAttribute("href") || "";
      if (/^https?:\/\/[^\s\u0000-\u001f]+$/i.test(href)) return `[${text}](${href})`;
    }
    return text;
  };
  return children(root);
}

function insertHtml(editor: HTMLDivElement, html: string) {
  // Native insertion keeps browser Undo working; the Range fallback also
  // supports browsers/test environments without the editing command.
  if (typeof document.execCommand === "function" && document.execCommand("insertHTML", false, html)) return;
  const selection = window.getSelection();
  if (!selection?.rangeCount || !editor.contains(selection.anchorNode)) return;
  const range = selection.getRangeAt(0);
  range.deleteContents();
  const template = document.createElement("template");
  template.innerHTML = html;
  const last = template.content.lastChild;
  range.insertNode(template.content);
  if (last) range.setStartAfter(last);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
}

export function DraftBodyEditor({ body, html, readOnly, onChange }: {
  body: string;
  html: string;
  readOnly: boolean;
  onChange: (body: string) => void;
}) {
  const editor = useRef<HTMLDivElement>(null);
  const displayedBody = useRef<string | null>(null);
  const composing = useRef(false);
  useLayoutEffect(() => {
    if (editor.current && displayedBody.current !== body && !composing.current) {
      editor.current.innerHTML = html;
      displayedBody.current = body;
    }
  }, [body, html]);
  const changed = () => {
    if (!editor.current || readOnly || composing.current) return;
    const next = markdownFromEditable(editor.current);
    displayedBody.current = next;
    onChange(next);
  };
  return <div ref={editor} className="studio-draft-preview studio-draft-editor studio-markdown"
    role="textbox" aria-label="Full post — headline and body" aria-multiline="true" aria-readonly={readOnly}
    contentEditable={!readOnly} suppressContentEditableWarning tabIndex={0}
    onInput={changed}
    onCompositionStart={() => { composing.current = true; }}
    onCompositionEnd={() => { composing.current = false; changed(); }}
    onClick={event => { if (!readOnly && (event.target as Element).closest("a")) event.preventDefault(); }}
    onPaste={event => {
      if (readOnly) return;
      event.preventDefault();
      const rich = event.clipboardData.getData("text/html");
      const host = document.createElement("template");
      host.innerHTML = rich;
      const text = rich ? markdownFromEditable(host.content) : event.clipboardData.getData("text/plain");
      insertHtml(event.currentTarget, htmlFromMarkdown(text));
      changed();
    }}
    onDragOver={event => { if (!readOnly) event.preventDefault(); }}
    onDrop={event => {
      // HTML/file drops must not insert unsanitized nodes into the editor.
      event.preventDefault();
    }} />;
}
