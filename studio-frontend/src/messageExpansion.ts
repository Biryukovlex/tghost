import { useLayoutEffect, useRef, useState } from "react";

export function useMessageExpansion(enabled: boolean) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [canExpand, setCanExpand] = useState(false);

  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!enabled || !content) return;
    const bubble = content.closest<HTMLElement>(".studio-message");
    const viewport = content.closest<HTMLElement>(".studio-viewport");
    const measure = () => {
      const hasTable = Boolean(content.querySelector("table"));
      const overflows = [content, ...content.querySelectorAll<HTMLElement>("*")].some((element) =>
        element instanceof HTMLElement && (
          (element.clientWidth > 0 && element.scrollWidth > element.clientWidth + 1) ||
          (element.clientHeight > 0 && element.scrollHeight > element.clientHeight + 1)
        ),
      );
      const viewportStyle = viewport ? getComputedStyle(viewport) : null;
      const availableHeight = viewport ? viewport.clientHeight
        - (parseFloat(viewportStyle!.paddingTop) || 0)
        - (parseFloat(viewportStyle!.paddingBottom) || 0) : 0;
      const tooTall = availableHeight > 0 && Boolean(bubble && bubble.getBoundingClientRect().height > availableHeight + 1);
      setCanExpand(hasTable || overflows || tooTall);
    };
    let frame = 0;
    const scheduleMeasure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };
    measure();
    // Markdown can render after the message mounts and change while streaming.
    const mutations = new MutationObserver(scheduleMeasure);
    mutations.observe(content, { subtree: true, childList: true, characterData: true, attributes: true });
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleMeasure);
    resize?.observe(content);
    if (bubble) resize?.observe(bubble);
    if (viewport) resize?.observe(viewport);
    window.addEventListener("resize", scheduleMeasure);
    return () => {
      cancelAnimationFrame(frame);
      mutations.disconnect();
      resize?.disconnect();
      window.removeEventListener("resize", scheduleMeasure);
    };
  }, [enabled]);

  return { contentRef, canExpand: enabled && canExpand };
}
