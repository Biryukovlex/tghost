// @vitest-environment node
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, expect, it, vi } from "vitest";

const appSource = readFileSync(new URL("../../app/web/static/app.js", import.meta.url), "utf8");
const uiControlsSource = readFileSync(new URL("../../app/web/static/ui-controls.js", import.meta.url), "utf8");

type Post = {
  id: number;
  channel_id: number;
  message_id: number;
  text: string;
  identifier: string;
  posted_at: string;
  views: number;
  reactions: number;
  comments: number;
  shares: number;
};

const posts: Post[] = [
  { id: 1, channel_id: 1, message_id: 101, text: "AI alpha post", identifier: "@alpha", posted_at: "2026-09-01T10:00", views: 100, reactions: 4, comments: 2, shares: 1 },
  { id: 2, channel_id: 2, message_id: 202, text: "Beta post", identifier: "@beta", posted_at: "2026-09-02T10:00", views: 200, reactions: 8, comments: 3, shares: 2 },
];

function metrics(series: Record<string, unknown> = {}) {
  return {
    current: { subscribers: null, views24: null, views48: null, views72: null, er: null, err: null },
    previous: null,
    coverage: {},
    observed_at: null,
    series: { labels: [], observed: [], subscribers: [], views24: [], views48: [], views72: [], er: [], err: [], ...series },
  };
}

function createOverview({
  url = "http://overview.test/",
  channel = "",
  lastFrom = "",
  lastTo = "",
  chart = { labels: ["2026-09-01", "2026-09-02"], views: [100, 200], reactions: [4, 8], comments: [2, 3], shares: [1, 2], postsPerDay: [1, 1] },
  channelMetrics = metrics(),
  delayQuery = "",
}: {
  url?: string;
  channel?: string;
  lastFrom?: string;
  lastTo?: string;
  chart?: Record<string, unknown>;
  channelMetrics?: ReturnType<typeof metrics>;
  delayQuery?: string;
} = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <form data-date-range-form data-last-from="${lastFrom}" data-last-to="${lastTo}">
      <select name="channel" data-initial-channel="${channel}">
        <option value="" ${channel ? "" : "selected"}>All active</option>
        <option value="1" ${channel === "1" ? "selected" : ""}>Long synthetic channel name for ellipsis checks</option>
      </select>
      <details class="date-popover" data-ui-popover open><summary>Date range</summary><fieldset>
        <input type="date" name="from" value="${lastFrom}"><input type="date" name="to" value="${lastTo}"><button type="submit">Apply dates</button>
        <a data-available-history href="/?days=all">Available history</a>
        <p data-date-error role="alert" hidden></p>
      </fieldset></details>
      <input type="hidden" name="days" value="14"><input type="hidden" name="order" value="date"><input type="hidden" name="page" value="1">
    </form>
    <div data-channel-metrics></div>
    <label><select id="chartMetric">
      <option value="views">Views</option><option value="reactions">Reactions</option><option value="comments">Comments</option><option value="shares">Shares</option>
      <option value="subscribers">Subscribers</option><option value="views24">Typical 24-hour views</option><option value="views48">Typical 48-hour views</option><option value="views72">Typical 72-hour views</option><option value="er">ER</option><option value="err">ERR</option>
    </select></label>
    <select id="chartMode"><option value="daily">Daily posts</option><option value="cumulative">Cumulative</option></select>
    <select id="chartPeriod"><option value="day">Day</option><option value="week">Week</option><option value="month">Month</option></select>
    <h2 data-trend-title></h2><p data-trend-scope></p>
    <div class="chart-wrap"><canvas id="trendChart" data-chart-scope="Selected dates" data-observation-scope="2026-09-01 – 2026-09-30"></canvas><p data-chart-empty role="status" hidden></p></div>
    <p data-chart-summary hidden></p><p data-chart-readout role="status"></p>
    <button data-chart-prev>Previous</button><button data-chart-next>Next</button>
    <button data-channel-metric="er">ER</button><button data-views-horizon="48">48h</button>
    <form data-explorer-form>
      <input type="search" name="q" aria-label="Search post text">
      <select name="sort"><option value="date">Newest</option><option value="views">Views</option><option value="reactions">Reactions</option><option value="comments">Comments</option><option value="shares">Shares</option></select>
      <details class="explorer-filter-popover" data-ui-popover open><summary>Filters</summary>
        <fieldset><input type="checkbox" name="channels" value="1" ${channel === "1" ? "checked" : ""} ${channel ? "disabled" : ""}><input type="checkbox" name="channels" value="2" ${channel ? "disabled" : ""}></fieldset>
        ${["views", "reactions", "comments", "shares"].map((key) => `<input type="number" name="min_${key}"><input type="number" name="max_${key}">`).join("")}
        <button type="submit">Apply filters</button><button type="button" data-explorer-clear>Clear</button>
      </details>
      <p id="explorerFilterError" data-explorer-error role="alert" hidden></p>
    </form>
    <div data-explorer-count></div><div data-explorer-chips></div><nav data-explorer-pages></nav><table class="explorer-table"><tbody data-explorer-rows></tbody></table>
  </body></html>`, { url, runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const win = window as unknown as Window & { Chart: new (_canvas: Element, config: any) => any; matchMedia: (query: string) => { matches: boolean }; __charts: any[] };
  Object.defineProperty(window, "matchMedia", { value: () => ({ matches: false }), configurable: true });
  const charts: any[] = [];
  win.__charts = charts;
  win.Chart = class {
    data: any;
    options: any;
    activeElements: any[] = [];
    constructor(_canvas: Element, config: any) { this.data = config.data; this.options = config.options; charts.push(this); }
    update() {}
    setActiveElements(elements: any[]) { this.activeElements = elements; }
  };
  window.document.querySelector("[data-channel-metrics]")!.setAttribute("data-channel-metrics", JSON.stringify(channelMetrics));
  window.document.getElementById("trendChart")!.setAttribute("data-chart", JSON.stringify(chart));
  const requests: URL[] = [];
  let releaseDelayed: ((value: any) => void) | null = null;
  window.fetch = ((input: RequestInfo | URL) => {
    const requestUrl = new URL(String(input), window.location.href);
    requests.push(requestUrl);
    const respond = () => {
      let result = posts;
      const fixed = requestUrl.searchParams.get("channel");
      const subset = (requestUrl.searchParams.get("channels") || "").split(",").filter(Boolean);
      if (fixed) result = result.filter((post) => post.channel_id === Number(fixed));
      else if (subset.length) result = result.filter((post) => subset.includes(String(post.channel_id)));
      const query = (requestUrl.searchParams.get("q") || "").toLowerCase();
      if (query) result = result.filter((post) => post.text.toLowerCase().includes(query));
      const totalPages = Math.max(1, Math.ceil(result.length / 50));
      const page = Math.min(Number(requestUrl.searchParams.get("page") || 1), totalPages);
      return { ok: true, json: async () => ({ rows: result.slice((page - 1) * 50, page * 50), total: result.length, page, total_pages: totalPages }) };
    };
    if (requestUrl.searchParams.get("q") === delayQuery && delayQuery) {
      return new Promise((resolve) => { releaseDelayed = resolve; });
    }
    return Promise.resolve(respond());
  }) as typeof window.fetch;
  window.eval(appSource);
  return { dom, window, requests, charts, releaseDelayed: () => releaseDelayed?.({ ok: true, json: async () => ({ rows: posts, total: posts.length, page: 1, total_pages: 1 }) }) };
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("Overview vanilla controller", () => {
  it("keeps Explorer requests inside a selected channel while All active subsets still narrow", async () => {
    const fixed = createOverview({ url: "http://overview.test/?channel=1", channel: "1" });
    await settle();
    expect(fixed.requests[0].searchParams.get("channel")).toBe("1");
    expect(fixed.requests[0].searchParams.has("channels")).toBe(false);
    const checkboxes = fixed.window.document.querySelectorAll<HTMLInputElement>('input[name="channels"]');
    checkboxes[0].checked = false;
    fixed.window.document.querySelector<HTMLButtonElement>('[data-explorer-clear]')!.click();
    await settle();
    expect(fixed.requests.at(-1)?.searchParams.get("channel")).toBe("1");
    expect(fixed.requests.at(-1)?.searchParams.has("channels")).toBe(false);
    expect(fixed.window.document.querySelectorAll('[data-explorer-chips] .chip')).toHaveLength(0);

    const narrowed = createOverview({ url: "http://overview.test/?channels=2" });
    await settle();
    expect(narrowed.requests[0].searchParams.get("channels")).toBe("2");
    expect(narrowed.window.document.querySelectorAll('[data-explorer-rows] tr')).toHaveLength(1);
    expect(narrowed.window.document.querySelector('[data-explorer-rows]')!.textContent).toContain("Beta post");
  });

  it("preserves typing focus and caret across debounced and late Explorer responses", async () => {
    const app = createOverview({ url: "http://overview.test/", delayQuery: "slow" });
    const { window } = app;
    const search = window.document.querySelector<HTMLInputElement>('input[name="q"]')!;
    const other = window.document.querySelector<HTMLButtonElement>('[data-explorer-clear]')!;
    search.focus();
    for (const value of ["A", "AI", "AI story"]) {
      search.value = value;
      search.setSelectionRange(value.length, value.length);
      search.dispatchEvent(new window.Event("input", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 280));
      await settle();
      expect(window.document.activeElement).toBe(search);
      expect(search.selectionStart).toBe(value.length);
    }
    search.value = "slow";
    search.dispatchEvent(new window.Event("input", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 280));
    other.focus();
    app.releaseDelayed();
    await settle();
    expect(window.document.activeElement).toBe(other);
    expect(search.value).toBe("slow");
  });

  it("restores bounded URL state, preserves it on date apply, and follows browser history", async () => {
    const app = createOverview({
      url: "http://overview.test/?channel=1&from=2026-09-01&q=AI&sort=views&order=views&page=2&min_views=100&chart_metric=er&chart_period=month",
      channel: "1",
    });
    await settle();
    const { window } = app;
    expect(window.document.querySelector<HTMLInputElement>('input[name="q"]')!.value).toBe("AI");
    expect(window.document.querySelector<HTMLSelectElement>('select[name="sort"]')!.value).toBe("views");
    expect(window.document.querySelector<HTMLInputElement>('input[name="min_views"]')!.value).toBe("100");
    expect(app.requests[0].searchParams.get("channel")).toBe("1");
    expect(app.requests[0].searchParams.get("page")).toBe("2");
    expect(new URL(window.location.href).searchParams.get("page")).toBeNull();
    expect(window.document.querySelector('[data-explorer-count]')!.textContent).toContain("page 1 of 1");
    expect(window.document.querySelector('[data-explorer-rows]')!.textContent).toContain("AI alpha post");
    expect(app.requests[0].searchParams.get("q")).toBe("AI");
    expect((window.document.getElementById("chartMetric") as HTMLSelectElement).value).toBe("er");
    expect((window.document.getElementById("chartPeriod") as HTMLSelectElement).value).toBe("month");

    const dateForm = window.document.querySelector<HTMLFormElement>("[data-date-range-form]")!;
    dateForm.addEventListener("submit", (event) => event.preventDefault());
    dateForm.querySelector<HTMLInputElement>('input[name="from"]')!.value = "2026-09-02";
    dateForm.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    expect(new window.FormData(dateForm).get("q")).toBe("AI");
    expect(new window.FormData(dateForm).get("sort")).toBe("views");
    expect(new window.FormData(dateForm).get("min_views")).toBe("100");
    expect(new window.FormData(dateForm).get("chart_metric")).toBe("er");
    expect(new window.FormData(dateForm).get("chart_period")).toBe("month");
    expect(new window.FormData(dateForm).get("page")).toBe("1");
    const historyLink = dateForm.querySelector<HTMLAnchorElement>("[data-available-history]")!;
    historyLink.addEventListener("click", (event) => event.preventDefault(), { once: true });
    historyLink.click();
    const historyUrl = new URL(historyLink.href);
    expect(historyUrl.searchParams.get("days")).toBe("all");
    expect(historyUrl.searchParams.get("channel")).toBe("1");
    expect(historyUrl.searchParams.get("q")).toBe("AI");
    expect(historyUrl.searchParams.get("sort")).toBe("views");
    expect(historyUrl.searchParams.get("min_views")).toBe("100");
    expect(historyUrl.searchParams.get("chart_metric")).toBe("er");
    expect(historyUrl.searchParams.get("chart_period")).toBe("month");
    expect(historyUrl.searchParams.has("from")).toBe(false);
    expect(historyUrl.searchParams.has("to")).toBe(false);

    const metric = window.document.getElementById("chartMetric") as HTMLSelectElement;
    metric.value = "shares";
    metric.dispatchEvent(new window.Event("change", { bubbles: true }));
    expect(new URL(window.location.href).searchParams.get("chart_metric")).toBe("shares");
    const popped = new Promise<void>((resolve) => window.addEventListener("popstate", () => resolve(), { once: true }));
    window.history.back();
    await popped;
    expect(metric.value).toBe("er");
    expect(window.document.querySelector<HTMLInputElement>('input[name="q"]')!.value).toBe("AI");
  });

  it("shows no-data states without a chart or fake point, keeps valid zero, and preserves measured gaps", async () => {
    const noPosts = createOverview({
      chart: { labels: ["2026-09-01", "2026-09-02"], views: [0, 0], reactions: [0, 0], comments: [0, 0], shares: [0, 0], postsPerDay: [0, 0] },
    });
    await settle();
    expect(noPosts.charts).toHaveLength(0);
    expect(noPosts.window.document.getElementById("trendChart")!.hidden).toBe(true);
    expect(noPosts.window.document.querySelector<HTMLParagraphElement>("[data-chart-empty]")!.hidden).toBe(false);
    expect(noPosts.window.document.querySelector("[data-chart-empty]")!.textContent).toContain("No posts are available in this view");
    expect(noPosts.window.document.querySelector("[data-chart-readout]")!.textContent).toContain("No measured chart points");
    expect(noPosts.window.document.querySelector<HTMLButtonElement>("[data-chart-prev]")!.disabled).toBe(true);

    const empty = createOverview({
      url: "http://overview.test/?channel=1&chart_metric=er",
      channel: "1",
      chart: { labels: ["2026-09-01", "2026-09-02"], views: [0, 0], reactions: [0, 0], comments: [0, 0], shares: [0, 0], postsPerDay: [0, 0] },
      channelMetrics: metrics({ labels: ["2026-09-01"], observed: [true], er: [null], subscribers: [null] }),
    });
    await settle();
    expect(empty.charts).toHaveLength(0);
    expect(empty.window.document.getElementById("trendChart")!.hidden).toBe(true);
    expect(empty.window.document.querySelector<HTMLParagraphElement>("[data-chart-empty]")!.hidden).toBe(false);
    expect(empty.window.document.querySelector<HTMLButtonElement>("[data-chart-next]")!.disabled).toBe(true);

    const zero = createOverview({
      url: "http://overview.test/?channel=1&chart_metric=er",
      channel: "1",
      channelMetrics: metrics({ labels: ["2026-09-01"], observed: [true], er: [0] }),
    });
    await settle();
    expect(zero.charts).toHaveLength(1);
    expect(zero.charts[0].data.datasets[0].data).toEqual([0]);
    expect(zero.charts[0].options.scales.y.beginAtZero).toBe(true);
    expect(zero.window.document.querySelector<HTMLElement>("[data-chart-empty]")!.hidden).toBe(true);
    expect(zero.window.document.querySelector("[data-chart-readout]")!.textContent).toContain("0.00%");

    const labels = Array.from({ length: 15 }, (_, index) => {
      const day = new Date(Date.UTC(2026, 8, 7 + index));
      return day.toISOString().slice(0, 10);
    });
    const subscribers: Array<number | null> = labels.map(() => null);
    subscribers[0] = 10000;
    subscribers[14] = 10200;
    const observed = labels.map((_, index) => index === 0 || index === 14);
    const gaps = createOverview({
      url: "http://overview.test/?channel=1&chart_metric=subscribers&chart_period=week",
      channel: "1",
      channelMetrics: metrics({ labels, observed, subscribers }),
    });
    await settle();
    expect(gaps.charts[0].data.datasets[0].data).toEqual([10000, null, 10200]);
    expect(gaps.charts[0].options.scales.y.beginAtZero).toBe(false);
    expect(gaps.charts[0].options.scales.y.min).toBeGreaterThan(0);
    expect(gaps.window.document.querySelector("[data-chart-readout]")!.textContent).toContain("point 1 of 2");
    gaps.window.document.querySelector<HTMLButtonElement>("[data-chart-next]")!.click();
    expect(gaps.window.document.querySelector("[data-chart-readout]")!.textContent).toContain("point 2 of 2");
    expect(gaps.charts[0].activeElements[0].index).toBe(2);

  });

  it("keeps invalid Explorer filter controls open and linked to their error", async () => {
    const app = createOverview();
    await settle();
    const { window } = app;
    const details = window.document.querySelector<HTMLDetailsElement>(".explorer-filter-popover")!;
    const min = window.document.querySelector<HTMLInputElement>('input[name="min_views"]')!;
    const max = window.document.querySelector<HTMLInputElement>('input[name="max_views"]')!;
    min.value = "100";
    max.value = "10";
    window.document.querySelector<HTMLButtonElement>('[data-explorer-form] button[type="submit"]')!.click();
    await settle();
    const error = window.document.querySelector<HTMLElement>("[data-explorer-error]")!;
    expect(details.open).toBe(true);
    expect(min.value).toBe("100");
    expect(max.value).toBe("10");
    expect(min.getAttribute("aria-invalid")).toBe("true");
    expect(min.getAttribute("aria-describedby")).toBe(error.id);
    expect(window.document.activeElement).toBe(min);
    expect(window.document.querySelectorAll('[data-explorer-rows] tr')).toHaveLength(2);

    max.value = "";
    min.value = "2147483648";
    window.document.querySelector<HTMLButtonElement>('[data-explorer-form] button[type="submit"]')!.click();
    await settle();
    expect(min.getAttribute("aria-invalid")).toBe("true");
    expect(window.document.activeElement).toBe(min);
    expect(error.textContent).toContain("2,147,483,647");
  });

  it("keeps date errors visible in a clipped popup and refits its scroll area", async () => {
    const app = createOverview({ lastFrom: "2026-09-01", lastTo: "2026-09-02" });
    const { window } = app;
    const popover = window.document.querySelector<HTMLDetailsElement>(".date-popover")!;
    const summary = popover.querySelector("summary")!;
    const body = popover.querySelector("fieldset")!;
    const rect = (top: number, bottom: number, height: number) => ({ top, bottom, height, left: 16, right: 336, width: 320, x: 16, y: top, toJSON: () => ({}) });
    Object.defineProperty(window, "innerHeight", { value: 320, configurable: true });
    summary.getBoundingClientRect = () => rect(260, 284, 24) as DOMRect;
    let bodyHeight = 190;
    body.getBoundingClientRect = () => rect(284, 284 + bodyHeight, bodyHeight) as DOMRect;
    window.eval(uiControlsSource);
    window.document.dispatchEvent(new window.Event("DOMContentLoaded", { bubbles: true }));
    popover.dispatchEvent(new window.Event("toggle", { bubbles: true }));
    expect(body.style.maxHeight).toBe("190px");

    const from = popover.querySelector<HTMLInputElement>('input[name="from"]')!;
    const to = popover.querySelector<HTMLInputElement>('input[name="to"]')!;
    from.value = "2026-09-04";
    to.value = "2026-09-02";
    bodyHeight = 280;
    const form = popover.closest("form")!;
    form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await settle();

    const error = popover.querySelector<HTMLElement>("[data-date-error]")!;
    expect(popover.open).toBe(true);
    expect(error.hidden).toBe(false);
    expect(error.textContent).toContain("must not be after");
    expect(from.getAttribute("aria-describedby")).toBe(error.id);
    expect(window.document.activeElement).toBe(from);
    expect(body.style.maxHeight).toBe("244px");
    expect(from.value).toBe("2026-09-01");
    expect(to.value).toBe("2026-09-02");
  });
});
