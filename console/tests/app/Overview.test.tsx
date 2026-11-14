import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { blocked, ciPark, DEMO, failed, fleet, merged, MINUTE, NOW, prPark, run } from "./needsFleet";

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
const needRows = () => [...screen.getByRole("region", { name: "Needs you" }).querySelectorAll<HTMLElement>("li[data-wait-row]")];

test("with three runs going and nothing waiting on the maintainer, the heading reads 3 running over Nothing needs you and no Needs you section", async () => {
  await mount("#/", fleet({ items: [], runs: threeRuns }, { items: [] }));
  expect(heading()).toBe("3 running");
  expect(screen.getByText("Nothing needs you")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Needs you" })).toBeNull();
  expect(sections()).toEqual(["Running", "Waiting", "Merged today"]);
});

test("with two items waiting on the maintainer, the heading reads 2 need you in --need and Needs you comes before Running", async () => {
  await mount("#/", fleet({ items: [blocked, ...failed], runs: threeRuns }, { items: [] }));
  expect(heading()).toBe("2 need you");
  const h1 = screen.getByRole("heading", { level: 1 });
  expect(classes(within(h1).getByText("2"))).toContain("text-need");
  expect(screen.queryByText("Nothing needs you")).toBeNull();
  expect(sections()).toEqual(["Needs you", "Running", "Waiting", "Merged today"]);
  expect(needRows()).toHaveLength(2);
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
  const tinted = (row: HTMLElement) => row.hasAttribute("data-open") && classes(row).includes("data-open:bg-(image:--need-tint)") && classes(row).includes("data-open:shadow-[inset_0_0_0_1px_var(--need-edge)]");
  expect(needRows().map(tinted)).toEqual([true, false]);
  const tintClasses = [...document.querySelectorAll("[class]")].flatMap((element) => classes(element).filter((name) => /need-(tint|edge)/.test(name)).map((name) => [element, name] as const));
  for (const [element, name] of tintClasses) {
    expect(name.startsWith("data-open:")).toBe(true);
    expect(needRows()).toContain(element as HTMLElement);
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

const ledgerOf = (count: number): Fetch => {
  const hosts = fleet({ items: [] }, { items: [] });
  const entries = Array.from({ length: count }, (_, index) => ({
    at: NOW - (index + 1) * MINUTE, run: index + 1, ticket: `DEMO-${index + 1}`, pr_url: null, kind: "merge", source: "loop", text: `MERGED to main as ${index + 1}.`,
  }));
  const ledger = (url: string) => Response.json({ active_outages: [], since: 0, limit: 200, entries: url.startsWith(`${DEMO}/`) ? entries : [] });
  return async (url, init) => (url.includes("/ledger?") ? ledger(url) : hosts(url, init));
};

const dayLog = () => screen.getByRole("complementary", { name: "Today's log" });

const STATE = { "first:": ":first-child", "last:": ":last-child", "not-last:": ":not(:last-child)" } as const;

function lineOf(item: Element): string[] {
  return classes(item).flatMap((name) => {
    if (name.startsWith("before:")) return [name.slice("before:".length)];
    const state = Object.entries(STATE).find(([prefix]) => name.startsWith(prefix + "before:"));
    if (!state || !item.matches(state[1])) return [];
    return [name.slice(state[0].length + "before:".length)];
  });
}

const SOLID = ["bg-rule"];
const FADE = ["bg-linear-to-b", "from-rule", "to-transparent"];
const fill = (line: string[]) => line.filter((name) => name.startsWith("bg-") || name.startsWith("from-") || name.startsWith("to-"));
const logItems = () => [...within(dayLog()).getByRole("list").children];

test("Today's log draws its line per entry, solid beside each entry and fading to transparent beside the last, from the first marker down", async () => {
  await mount("#/", ledgerOf(2));
  expect(classes(within(dayLog()).getByRole("list")).filter((name) => name.startsWith("border"))).toEqual([]);
  const [first, last] = logItems();
  expect(logItems()).toHaveLength(2);
  expect(fill(lineOf(first!))).toEqual(SOLID);
  expect(fill(lineOf(last!))).toEqual(FADE);
  for (const item of logItems()) {
    expect(classes(item)).toContain("relative");
    expect(lineOf(item)).toEqual(expect.arrayContaining(["absolute", "-left-px", "w-px", "inset-y-0"]));
    expect(classes(item.querySelector("[data-tone]"))).toEqual(expect.arrayContaining(["top-3", "-left-1"]));
  }
  expect(lineOf(first!)).toContain("top-3");
  expect(lineOf(last!)).not.toContain("top-3");
});

test("with an and N earlier today line, that line fades and every entry above it is solid", async () => {
  await mount("#/", ledgerOf(27));
  const items = logItems();
  const earlier = items.at(-1)!;
  expect(earlier.textContent).toBe("and 2 earlier today");
  expect(fill(lineOf(earlier))).toEqual(FADE);
  for (const item of items.slice(0, -1)) expect(fill(lineOf(item))).toEqual(SOLID);
});

test("a one-entry log's line fades and starts at its marker", async () => {
  await mount("#/", ledgerOf(1));
  const [only] = logItems();
  expect(logItems()).toHaveLength(1);
  expect(fill(lineOf(only!))).toEqual(FADE);
  expect(lineOf(only!)).toContain("top-3");
});
