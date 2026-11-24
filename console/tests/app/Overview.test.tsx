import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { ShippedRow, Status } from "../../src/lib/contract.gen";
import { fakeDeps, peersFetch, settle } from "../harness";
import { blocked, ciPark, DEMO, failed, fleet, merged, MINUTE, NOW, prPark, run, SERVE, WRITER } from "./needsFleet";

async function mount(hash: string, fetchImpl: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

const heading = () => screen.queryByRole("heading", { level: 1 })?.textContent ?? null;
const sections = () => screen.getAllByRole("region").map((region) => region.getAttribute("aria-label"));
const classes = (element: Element | null | undefined) => element?.getAttribute("class")?.split(/\s+/) ?? [];
const threeRuns = [run(3, "DEMO-3"), run(7, "DEMO-7"), run(8, "DEMO-8")];
const figureType = ["text-[64px]", "font-[220]", "font-stretch-125%", "tracking-[-0.03em]", "leading-[0.9]"];
const needRows = () => [...screen.getByRole("region", { name: "Needs you" }).querySelectorAll<HTMLElement>("li[data-wait-row]")];

test("with three runs going and nothing waiting on the maintainer, the heading reads 3 running in the light, wide figure over Nothing needs you and no Needs you section", async () => {
  await mount("#/", fleet({ items: [], runs: threeRuns }, { items: [] }));
  expect(heading()).toBe("3 running");
  const h1 = screen.getByRole("heading", { level: 1 });
  expect(classes(within(h1).getByText("3"))).toEqual(expect.arrayContaining(figureType));
  expect(classes(within(h1).getByText("running"))).toEqual(expect.arrayContaining(["text-[22px]", "font-normal", "font-stretch-112%"]));
  expect(screen.getByText("Nothing needs you")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Needs you" })).toBeNull();
  expect(sections()).toEqual(["Running", "Waiting", "Merged today"]);
});

test("with two items waiting on the maintainer, the heading reads 2 need you in the light, wide figure in --need and Needs you comes before Running", async () => {
  await mount("#/", fleet({ items: [blocked, ...failed], runs: threeRuns }, { items: [] }));
  expect(heading()).toBe("2 need you");
  const h1 = screen.getByRole("heading", { level: 1 });
  expect(classes(within(h1).getByText("2"))).toEqual(expect.arrayContaining([...figureType, "text-need"]));
  expect(screen.queryByText("Nothing needs you")).toBeNull();
  expect(sections()).toEqual(["Needs you", "Running", "Waiting", "Merged today"]);
  expect(needRows()).toHaveLength(2);
});

test("the console's Archivo request covers weight 220 and width 125", async () => {
  const html = await Bun.file(new URL("../../index.html", import.meta.url)).text();
  const href = /href="(https:\/\/fonts\.googleapis\.com\/css2\?[^"]+)"/.exec(html)?.[1].replaceAll("&amp;", "&");
  const family = new URL(href!).searchParams.getAll("family").find((spec) => spec.startsWith("Archivo:"));
  const [axes, ranges] = family!.slice("Archivo:".length).split("@");
  const range = Object.fromEntries(axes.split(",").map((axis, index) => [axis, ranges.split(",")[index].split("..").map(Number)]));
  expect(range.wght[0]).toBeLessThanOrEqual(220);
  expect(range.wdth[1]).toBeGreaterThanOrEqual(125);
});

test("the glyph counts are images labelled with the running, pull request, queued and merged today counts", async () => {
  const merges = Array.from({ length: 25 }, (_, index) => merged(100 + index, `DEMO-${100 + index}`, NOW - (index + 1) * 10 * MINUTE));
  await mount("#/", fleet({ items: [ciPark], runs: threeRuns, shipped: merges }, { items: [prPark] }));
  for (const name of ["3 running", "2 waiting on a pull request", "0 queued", "25 merged today"]) {
    const glyph = screen.getByRole("img", { name });
    expect(glyph.getAttribute("title")).toBe(name);
  }
  expect(within(screen.getByRole("region", { name: "Waiting" })).getAllByRole("listitem")).toHaveLength(2);
});

test("with five merges today, Merged today lists the three newest and a 2 more link to Runs filtered to merged runs started today", async () => {
  const merges = [
    merged(11, "DEMO-11", NOW - 50 * MINUTE),
    merged(14, "DEMO-14", NOW - 20 * MINUTE),
    merged(12, "DEMO-12", NOW - 40 * MINUTE),
    merged(15, "DEMO-15", NOW - 10 * MINUTE),
    merged(13, "DEMO-13", NOW - 30 * MINUTE),
  ];
  await mount("#/", fleet({ items: [], shipped: merges }, { items: [] }));
  const done = screen.getByRole("region", { name: "Merged today" });
  expect([...done.querySelectorAll("li[data-merge]")].map((row) => row.getAttribute("data-merge"))).toEqual(["DEMO-15", "DEMO-14", "DEMO-13"]);
  const more = within(done).getByRole("link", { name: "2 more" });
  await act(async () => {
    fireEvent.click(more);
  });
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/runs?state=merged&range=today");
  expect(screen.getByRole("heading", { level: 1, name: "Runs" })).toBeTruthy();
});

test("on a picked project, the more link counts that project's merges and keeps the scope on Runs", async () => {
  const merges = (prefix: string, count: number) => Array.from({ length: count }, (_, index) => merged(20 + index, `${prefix}-${20 + index}`, NOW - (index + 1) * 10 * MINUTE));
  await mount("#/?project=demo", fleet({ items: [], shipped: merges("DEMO", 5) }, { items: [], shipped: merges("WRITER", 4) }));
  const more = within(screen.getByRole("region", { name: "Merged today" })).getByRole("link", { name: "2 more" });
  await act(async () => {
    fireEvent.click(more);
  });
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(Object.fromEntries(new URLSearchParams(window.location.hash.split("?")[1]))).toEqual({ project: "demo", state: "merged", range: "today" });
});

test("with two Needs you rows and the first opened, only the opened row carries the need tint", async () => {
  await mount("#/", fleet({ items: [blocked, ...failed] }, { items: [] }));
  const tint = ["bg-(image:--need-tint)", "shadow-[inset_0_0_0_1px_var(--need-edge)]"];
  const tinted = (row: HTMLElement) => row.hasAttribute("data-open") && tint.every((name) => classes(row).includes(`data-open:${name}`));
  expect(needRows().map(tinted)).toEqual([true, false]);
  const tintClasses = [...document.querySelectorAll("[class]")].flatMap((element) => classes(element).filter((name) => /need-(tint|edge)/.test(name)).map((name) => [element, name] as const));
  for (const [element, name] of tintClasses) {
    expect(needRows()).toContain(element as HTMLElement);
    expect(name).toStartWith("data-open:");
  }
  fireEvent.click(within(needRows()[1]!).getAllByRole("button")[0]!);
  expect(needRows().map(tinted)).toEqual([false, true]);
});

test("a ready pull request is a Waiting row, not a Needs you item, and a thread or question park is a Needs you item, not a Waiting row", async () => {
  const waitingKeys = () => [...screen.getByRole("region", { name: "Waiting" }).querySelectorAll("[data-pr-row]")].map((row) => row.textContent!.match(/[A-Z]+-\d+/)![0]);
  await mount("#/", fleet({ items: [] }, { items: [prPark] }));
  expect(heading()).toBe("0 running");
  expect(waitingKeys()).toEqual(["WRITER-9"]);
  cleanup();

  const thread = { ...blocked, ticket: "DEMO-5", run: 5, park_kind: "thread", pr_url: "https://github.com/example/demo/pull/55" };
  await mount("#/", fleet({ items: [blocked, thread] }, { items: [] }));
  expect(heading()).toBe("2 need you");
  expect(needRows().map((row) => row.getAttribute("data-wait-row")).sort()).toEqual(["DEMO-4", "DEMO-5"]);
  expect(screen.getByRole("region", { name: "Waiting" }).querySelectorAll("[data-pr-row]")).toHaveLength(0);
});

test("a picked project scopes the heading and the glyph counts", async () => {
  const busy = fleet({ items: [blocked, ciPark], runs: [run(3, "DEMO-3")] }, { items: [prPark], runs: [run(9, "WRITER-9")] });
  await mount("#/", busy);
  expect(screen.getByRole("img", { name: "2 running" })).toBeTruthy();
  expect(screen.getByRole("img", { name: "2 waiting on a pull request" })).toBeTruthy();
  cleanup();

  await mount("#/?project=demo", busy);
  expect(heading()).toBe("1 need you");
  expect(screen.getByRole("img", { name: "1 running" })).toBeTruthy();
  expect(screen.getByRole("img", { name: "1 waiting on a pull request" })).toBeTruthy();
});

test("before any scoped host answers there is no heading and the sections stand without counts or rows", async () => {
  await mount("#/", () => new Promise<Response>(() => {}));
  expect(heading()).toBeNull();
  expect(screen.queryByText("Nothing needs you")).toBeNull();
  expect(sections()).toEqual(["Running", "Waiting", "Merged today"]);
  for (const region of screen.getAllByRole("region")) {
    expect(region.querySelector("[data-count]")).toBeNull();
    expect(region.querySelector("li")).toBeNull();
  }
});

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const LOTUS = "http://runner-host:7710";
const WEDNESDAY = new Date(2026, 9, 14, 15, 0).getTime();
const on = (date: number, hour: number, minute = 0) => new Date(2026, 9, date, hour, minute).getTime();

/** holophyte on DEMO, croton on WRITER and lotuspod on LOTUS, each paging
 *  its `/shipped` merges from `pages`; every `/shipped` url asked is kept. */
function weekFleet(pages: Record<string, ShippedRow[][]>, asked: string[] = []): Fetch {
  const projects: Record<string, string> = { [DEMO]: "/srv/dev/holophyte", [WRITER]: "/srv/dev/croton", [LOTUS]: "/srv/dev/lotuspod" };
  const polls = peersFetch(DEMO, Object.fromEntries(Object.entries(projects).map(([base, project]) => [base, {
    status: { ...working, project, runs: [], queued: [] },
    attention: { level: "none", now: WEDNESDAY, project, items: [] },
  }])));
  return async (url) => {
    const base = Object.keys(projects).find((candidate) => url.startsWith(`${candidate}/shipped?`));
    if (base == null) return polls(url);
    asked.push(url);
    const own = pages[base] ?? [[]];
    const before = new URL(url).searchParams.get("before");
    const at = before == null ? 0 : own.findIndex((page) => String(page.at(-1)?.id) === before) + 1;
    const page = own[at] ?? [];
    return Response.json({ rows: page, next_before: at < own.length - 1 ? page.at(-1)!.id : null, limit: 200 });
  };
}

const mergesOn = (first: number, ticket: string, count: number, endedMs: number) =>
  Array.from({ length: count }, (_, index) => merged(first + index, `${ticket}-${first + index}`, endedMs - index * MINUTE));

async function mountWeek(hash: string, fetchImpl: Fetch, now = WEDNESDAY) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = now;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const weekTotal = () => within(screen.getByRole("heading", { name: "This week" }).parentElement!).getByText(/merged$/).textContent;
const dayButton = (day: string) => screen.getByRole("button", { name: new RegExp(`^${day}, `) });
const barOf = (day: string) => dayButton(day).querySelector<HTMLElement>("[data-bar]");
const segmentsOf = (day: string) => [...barOf(day)!.querySelectorAll<HTMLElement>("[data-segment]")].map((segment) => [segment.getAttribute("data-segment"), segment.style.flexGrow, segment.style.background]);

test("This week scales each day's bar to the busiest day, stacks it by project in each project's colour and totals the week", async () => {
  const wednesday = [...mergesOn(10, "HOLO", 6, on(14, 11)), ...mergesOn(30, "HOLO", 2, on(12, 10))];
  await mountWeek("#/", weekFleet({ [DEMO]: [wednesday], [WRITER]: [mergesOn(50, "CROTON", 2, on(14, 9))] }));
  expect(barOf("Wednesday")!.style.height).toBe("64px");
  expect(segmentsOf("Wednesday")).toEqual([["holophyte", "6", "var(--p-violet)"], ["croton", "2", "var(--p-teal)"]]);
  expect(barOf("Monday")!.style.height).toBe("16px");
  expect(segmentsOf("Monday")).toEqual([["holophyte", "2", "var(--p-violet)"]]);
  expect(weekTotal()).toBe("10 merged");
});

test("a day far below the busiest keeps its share of the height, its segments sharing it from no base and clipped to it", async () => {
  const busy = { [DEMO]: [[...mergesOn(10, "HOLO", 100, on(13, 20)), merged(5, "HOLO-5", on(12, 11))]], [WRITER]: [[merged(6, "CROTON-6", on(12, 10))]], [LOTUS]: [[merged(7, "LOTUS-7", on(12, 9))]] };
  await mountWeek("#/", weekFleet(busy));
  expect(barOf("Monday")!.style.height).toBe("1.92px");
  expect(barOf("Monday")!.style.overflow).toBe("hidden");
  const segments = [...barOf("Monday")!.querySelectorAll<HTMLElement>("[data-segment]")];
  expect(segments.map((segment) => [segment.getAttribute("data-segment"), segment.style.flexBasis, segment.style.minHeight])).toEqual([["holophyte", "0px", ""], ["croton", "0px", ""], ["lotuspod", "0px", ""]]);
});

test("This week starts at local Monday's midnight: a Sunday 23:30 merge on the next page is left out and no later page is asked", async () => {
  const asked: string[] = [];
  const monday = merged(21, "HOLO-21", on(12, 0, 30));
  const sunday = merged(20, "HOLO-20", on(11, 23, 30));
  const older = merged(19, "HOLO-19", on(10, 12));
  await mountWeek("#/", weekFleet({ [DEMO]: [[monday], [sunday], [older]] }, asked));
  expect(weekTotal()).toBe("1 merged");
  expect(dayButton("Monday").getAttribute("aria-label")).toBe("Monday, 1 merged: holophyte 1");
  expect(asked.filter((url) => url.startsWith(`${DEMO}/shipped?limit=200`))).toEqual([`${DEMO}/shipped?limit=200`, `${DEMO}/shipped?limit=200&before=21`]);
});

test("on a Wednesday, Thursday to Sunday draw a stub and no bar, and only Wednesday's letter is in --ink at weight 600", async () => {
  await mountWeek("#/", weekFleet({ [DEMO]: [mergesOn(10, "HOLO", 3, on(14, 11))] }));
  for (const day of ["Thursday", "Friday", "Saturday", "Sunday"]) {
    expect(barOf(day)).toBeNull();
    expect(dayButton(day).querySelector("[data-stub]")).not.toBeNull();
  }
  const bold = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].filter((day) => {
    const letter = classes(dayButton(day).querySelector("[data-day-letter]"));
    return letter.includes("text-ink") && letter.includes("font-semibold");
  });
  expect(bold).toEqual(["Wednesday"]);
});

test("each day's button names its merges by project, its card shows while hovered or focused, and a picked project counts its own alone", async () => {
  const tuesday = { [DEMO]: [mergesOn(10, "HOLO", 18, on(13, 18))], [WRITER]: [mergesOn(40, "CROTON", 9, on(13, 17))], [LOTUS]: [mergesOn(60, "LOTUS", 4, on(13, 16))] };
  await mountWeek("#/", weekFleet(tuesday));
  const button = screen.getByRole("button", { name: "Tuesday, 31 merged: holophyte 18, croton 9, lotuspod 4" });
  expect(button.textContent).not.toContain("croton");
  fireEvent.mouseEnter(button);
  expect(button.textContent).toContain("Tuesday 31 merged");
  expect(button.textContent).toContain("holophyte18croton9lotuspod4");

  fireEvent.focus(button);
  fireEvent.mouseLeave(button);
  expect(button.textContent).toContain("Tuesday 31 merged");
  fireEvent.blur(button);
  expect(button.textContent).not.toContain("croton");
  cleanup();

  await mountWeek("#/?project=croton", weekFleet(tuesday));
  expect(dayButton("Tuesday").getAttribute("aria-label")).toBe("Tuesday, 9 merged: croton 9");
  expect(weekTotal()).toBe("9 merged");
});

const column = (name: string) => document.querySelector<HTMLElement>(`[data-overview-column="${name}"]`)!;
const regionsIn = (container: HTMLElement) => within(container).queryAllByRole("region").map((region) => region.getAttribute("aria-label"));
const threeColumns = "min-[1680px]:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)_380px]";

test("the Overview's three column containers hold now, landed and the log in order, under three-column grid classes that all carry the min-[1680px] prefix", async () => {
  const merges = [merged(11, "DEMO-11", NOW - 20 * MINUTE), merged(12, "DEMO-12", NOW - 10 * MINUTE)];
  await mount("#/", fleet({ items: [ciPark], runs: threeRuns, shipped: merges }, { items: [prPark] }));
  const columns = [...document.querySelectorAll<HTMLElement>("[data-overview-column]")];
  expect(columns.map((container) => container.getAttribute("data-overview-column"))).toEqual(["now", "landed", "log"]);
  expect(regionsIn(column("now"))).toEqual(["Running", "Waiting"]);
  const landed = column("landed");
  expect(regionsIn(landed)).toEqual(["Merged today"]);
  const week = within(landed).getByRole("heading", { name: "This week" });
  const mergedToday = within(landed).getByRole("region", { name: "Merged today" });
  expect(week.compareDocumentPosition(mergedToday) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(column("log")).getByRole("complementary", { name: "Today" })).toBeTruthy();

  const grid = classes(columns[0]!.parentElement);
  expect(grid).toContain(threeColumns);
  expect(grid.filter((name) => name.includes("1.15fr"))).toEqual([threeColumns]);
});

test("below 1680px, landed dissolves so Merged today stays in the first column under Waiting and This week stays in the second column above the Today log", async () => {
  await mount("#/", fleet({ items: [ciPark], runs: threeRuns, shipped: [merged(11, "DEMO-11", NOW - 20 * MINUTE)] }, { items: [prPark] }));
  const landed = column("landed");
  const log = column("log");
  const weekWrap = within(landed).getByRole("heading", { name: "This week" }).closest("[data-week]")!.parentElement!;
  const mergedWrap = within(landed).getByRole("region", { name: "Merged today" }).parentElement!;
  expect([...landed.children]).toEqual([weekWrap, mergedWrap]);

  expect(classes(landed)).toEqual(expect.arrayContaining(["contents", "min-[1680px]:col-start-2", "min-[1680px]:flex", "min-[1680px]:flex-col", "min-[1680px]:gap-10"]));
  expect(classes(column("now"))).toEqual(expect.arrayContaining(["col-start-1", "row-start-1"]));
  expect(classes(mergedWrap)).toEqual(expect.arrayContaining(["col-start-1", "row-start-2", "mt-10", "min-[1680px]:mt-0"]));
  expect(classes(weekWrap)).toEqual(expect.arrayContaining(["col-start-2", "row-span-full", "min-[1200px]:sticky", "min-[1200px]:block", "min-[1680px]:static"]));
  expect(classes(weekWrap)).not.toContain("mt-10");
  expect(classes(log)).toEqual(expect.arrayContaining(["col-start-2", "row-span-full", "pt-[calc(var(--week-bars-height,0px)+2rem)]", "min-[1680px]:col-start-3", "min-[1680px]:pt-0"]));
});

test("when someone is needed, Needs you sits in the first column above Running", async () => {
  await mount("#/", fleet({ items: [blocked, ...failed], runs: threeRuns }, { items: [] }));
  expect(regionsIn(column("now"))).toEqual(["Needs you", "Running", "Waiting"]);
});

test("This week and Today's log each render once on the Overview", async () => {
  await mount("#/", fleet({ items: [], runs: threeRuns, shipped: [merged(11, "DEMO-11", NOW - 20 * MINUTE)] }, { items: [] }));
  expect(screen.getAllByRole("heading", { name: "This week" })).toHaveLength(1);
  expect(screen.getAllByRole("complementary", { name: "Today" })).toHaveLength(1);
});

test("the Overview's header opens the now column above its first section, beside landed and the log starting at the grid's first row", async () => {
  await mount("#/", fleet({ items: [blocked], runs: threeRuns }, { items: [] }));
  const now = column("now");
  const head = document.querySelector<HTMLElement>("[data-overview-head]")!;
  expect(now.contains(head)).toBe(true);
  const firstSection = within(now).getAllByRole("region")[0]!;
  expect(firstSection.getAttribute("aria-label")).toBe("Needs you");
  expect(head.compareDocumentPosition(firstSection) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const grid = now.parentElement!;
  expect([column("landed").parentElement, column("log").parentElement]).toEqual([grid, grid]);
  expect(classes(now)).toContain("row-start-1");
  expect(classes(column("landed"))).toContain("min-[1680px]:row-start-1");
  expect(classes(column("log"))).toContain("row-span-full");
});

test("before any host answers, the first-load line opens the now column above Running", async () => {
  await mount("#/", () => new Promise<Response>(() => {}));
  const now = column("now");
  const asking = within(now).getAllByRole("status").find((line) => line.textContent?.startsWith("Asking"))!;
  const running = within(now).getByRole("region", { name: "Running" });
  expect(regionsIn(now)[0]).toBe("Running");
  expect(asking.compareDocumentPosition(running) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
