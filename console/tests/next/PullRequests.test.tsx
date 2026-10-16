import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { Attention, RunDetail, Status } from "../../src/lib/contract.gen";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import asks from "../../../tests/fixtures/serve/run-asks.json";
import ready from "../../../tests/fixtures/serve/run-merge-ready.json";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const served = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const detail = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;

const HOUR = 3_600_000;
const NOW = new Date(2026, 9, 15, 16, 30).getTime();

type Item = Attention["items"][number];
const park = (run: number, parkKind: string, ageMs: number, pr: object): Item => ({
  kind: "pr_open", level: "attention", run, ticket: `DEMO-${run}`, ticket_url: null, title: `Ticket ${run}`,
  park_kind: parkKind, reason: "PR open", asked_ms: NOW - ageMs,
  pr_url: `https://github.com/example/demo/pull/${run + 33}`, pr: { title: null, ...pr },
} as Item);
const onCi = park(8, "ci", 3 * HOUR, { number: 41, checks: "pending", review: null, threads: 0, open_threads: 0 });
const readyPark = park(9, "pull_request", 2 * HOUR, { number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0 });
const threadPark = park(10, "pull_request", HOUR, { number: 99, checks: "success", review: "approved", threads: 1, open_threads: 1 });
const blocked = {
  kind: "blocked", level: "attention", ticket: "DEMO-4", ticket_url: null, question: "Which way?", run: 4,
  asked_ms: NOW - 5 * HOUR, pr_url: null,
} as Item;

interface Daemon {
  items: Item[];
  actions?: boolean;
  readyRuns?: number[];
}

let client: QueryClient | null = null;

async function mount({ items, actions = false, readyRuns = [9] }: Daemon) {
  const statusOf = (project: string): Status => ({ ...served, project, now: NOW, runs: [], queued: [], actions });
  const attention = (project: string, list: Item[]) => ({ level: list.length ? "attention" : "none", now: NOW, project, items: list });
  const fleet = peersFetch(DEMO, {
    [DEMO]: { status: statusOf("/srv/dev/demo"), attention: attention("/srv/dev/demo", items) },
    [WRITER]: { status: statusOf("/srv/dev/writer"), attention: attention("/srv/dev/writer", []) },
  });
  const { seen, fetchImpl } = fakeFetch({});
  const routed: typeof fetchImpl = async (url, init) => {
    const merge = url.match(/\/runs\/(\d+)\/merge$/);
    if (merge) return Response.json({ ...ready, run: Number(merge[1]), ready: readyRuns.includes(Number(merge[1])) });
    if (/\/runs\/\d+\/asks$/.test(url) || /\/runs\/\d+$/.test(url)) {
      await fetchImpl(url, init);
      return Response.json(url.endsWith("/asks") ? { ...asks, asks: asks.asks.slice(0, 1) } : detail);
    }
    return fleet(url, init);
  };
  window.location.hash = "#/";
  const fake = fakeDeps(routed);
  fake.clock.now = NOW;
  client = consoleQueryClient();
  render(
    <QueryClientProvider client={client}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  return seen;
}

const section = () => screen.getByRole("region", { name: "Pull requests" });
const lane = (name: string) => within(section()).getByRole("group", { name });
const rows = () => [...section().querySelectorAll<HTMLElement>("[data-pr-row]")];
const states = () => rows().map((row) => row.querySelector("[data-pr-state]")!.textContent);
const facts = (row: Element) => row.querySelector("[data-facts]")!.textContent;
const rowButton = (pr: number) => within(section()).getByRole("button", { name: new RegExp(`^PR #${pr}, `) });

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  client?.clear();
  client = null;
  window.location.hash = "";
});

test("a lane per project counts its open pull requests, longest waiting first, each row stating where it waits", async () => {
  await mount({ items: [threadPark, readyPark, onCi] });
  expect(lane("demo").querySelector("[data-lane-head]")!.textContent).toContain("3 open");
  expect(lane("writer").textContent).toContain("No pull requests open.");
  expect(within(lane("writer")).queryAllByRole("listitem")).toEqual([]);
  expect(states()).toEqual(["on CI · checks running", "ready to merge", "parked · 1 thread open"]);
});

test("a row names its pull request from its URL and lists its three facts, unknown ones as unknown", async () => {
  const unpolled = park(11, "pull_request", 30 * 60_000, { number: null, checks: null, review: null, threads: null, open_threads: null });
  await mount({ items: [onCi, readyPark, threadPark, unpolled] });
  const [, , third, fourth] = rows();
  expect(within(third!).getByText("PR #43")).toBeTruthy();
  expect(facts(third!)).toBe("checks green · approved · 1 thread open");
  expect(facts(fourth!)).toBe("checks unknown · review unknown · threads unknown");
  expect(fourth!.querySelector("[data-pr-state]")!.textContent).toBe("parked · checks unknown");
});

test("only the ready park points back to Needs you, and pressing it makes it the focused card", async () => {
  await mount({ items: [blocked, onCi, readyPark, threadPark] });
  const card = () => screen.getByRole("article", { name: "Focus" });
  expect(card().getAttribute("data-focus")).toBe("DEMO-4");
  const tags = within(section()).getAllByRole("button", { name: /in Needs you$/ });
  expect(tags.map((tag) => tag.getAttribute("aria-label"))).toEqual(["Show DEMO-9 in Needs you"]);
  await act(async () => {
    fireEvent.click(tags[0]!);
    await settle();
  });
  expect(card().getAttribute("data-focus")).toBe("DEMO-9");
  expect(document.activeElement).toBe(card());
});

test("an opened row shows its run's detail, and Ask only where the daemon takes actions", async () => {
  const seen = await mount({ items: [onCi, readyPark, threadPark], actions: true, readyRuns: [] });
  expect(seen.map((request) => request.url)).toEqual([]);
  await act(async () => {
    fireEvent.click(rowButton(43));
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
  const row = rows()[2]!;
  expect(rowButton(43).getAttribute("aria-expanded")).toBe("true");
  expect(within(row).getByRole("region", { name: "Pull request facts" })).toBeTruthy();
  expect(within(row).getByRole("button", { name: "Ask" })).toBeTruthy();
  expect(within(row).getByRole("link", { name: "Open run 10" }).getAttribute("href")).toBe(`/?classic#run=10&daemon=${encodeURIComponent(DEMO)}`);
  const details = seen.map((request) => request.url).filter((url) => /\/runs\/\d+$/.test(url));
  expect([...new Set(details)]).toEqual([`${DEMO}/runs/10`]);
  cleanup();
  client?.clear();

  await mount({ items: [onCi, readyPark, threadPark], actions: false, readyRuns: [] });
  await act(async () => {
    fireEvent.click(rowButton(43));
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
  const opened = rows()[2]!;
  expect(within(opened).getByRole("region", { name: "Pull request facts" })).toBeTruthy();
  expect(within(opened).queryByRole("button", { name: "Ask" })).toBeNull();
});

test("the PR number links to GitHub without toggling or fetching the row detail", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [] });
  const link = within(rows()[0]!).getByRole("link", { name: "PR #42" });
  const button = rowButton(42);
  expect(link.getAttribute("href")).toBe("https://github.com/example/demo/pull/42");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noreferrer");
  expect(link.closest("button")).toBeNull();
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(seen.filter(({ url }) => /\/runs\/\d+$/.test(url))).toEqual([]);
  await act(async () => {
    fireEvent.click(button);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(seen.some(({ url }) => url === `${DEMO}/runs/9`)).toBe(true);
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
});

test("with storage refusing, the tag reopens a folded Needs you and focuses its card", async () => {
  await mount({ items: [blocked, onCi, readyPark, threadPark] });
  const storage = Object.getPrototypeOf(localStorage) as Storage;
  const { getItem, setItem, removeItem } = storage;
  const refuse = () => {
    throw new DOMException("storage disabled", "SecurityError");
  };
  storage.getItem = storage.setItem = storage.removeItem = refuse;
  try {
    const needs = screen.getByRole("region", { name: "Needs you" });
    await act(async () => {
      fireEvent.click(within(needs).getByRole("button", { name: "Needs you" }));
      await settle();
    });
    expect(screen.queryByRole("article", { name: "Focus" })).toBeNull();
    await act(async () => {
      fireEvent.click(within(section()).getByRole("button", { name: "Show DEMO-9 in Needs you" }));
      await settle();
    });
    const card = screen.getByRole("article", { name: "Focus" });
    expect(card.getAttribute("data-focus")).toBe("DEMO-9");
    expect(document.activeElement).toBe(card);
  } finally {
    Object.assign(storage, { getItem, setItem, removeItem });
  }
});
