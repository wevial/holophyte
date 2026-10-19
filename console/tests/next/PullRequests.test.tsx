import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { Attention, Status } from "../../src/lib/contract.gen";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import ready from "../../../tests/fixtures/serve/run-merge-ready.json";
import ticketDetail from "../../../tests/fixtures/serve/ticket.json";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const served = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const NOW = new Date(2026, 9, 15, 16, 30).getTime();

type Item = Attention["items"][number];
const park = (run: number, parkKind: string, ageMs: number, pr: object): Item => ({
  kind: "pr_open", level: "attention", run, ticket: `DEMO-${run}`, ticket_url: null, title: `Ticket ${run}`,
  park_kind: parkKind, reason: "PR open", asked_ms: NOW - ageMs,
  pr_url: `https://github.com/example/demo/pull/${run + 33}`, pr: { title: null, state: "ready", number: run + 33, ...pr },
} as Item);
const onCi = park(8, "ci", 3 * HOUR, { number: 41, checks: "pending", review: null, threads: 0, open_threads: 0 });
const readyPark = park(9, "pull_request", 2 * HOUR, { number: 42, checks: "success", review: "approved", threads: 0, open_threads: 0 });
const threadPark = park(10, "pull_request", HOUR, { number: 99, checks: "success", review: "approved", threads: 1, open_threads: 1 });
const blocked = {
  kind: "blocked", level: "attention", ticket: "DEMO-4", ticket_url: null, question: "Which way?", run: 4,
  asked_ms: NOW - 5 * HOUR, pr_url: null, park_kind: null, pr: null,
} as Item;
const PR_42 = "https://github.com/example/demo/pull/42";
const WHAT = "## What / Why / How\n\n**What:** Adds a slot that takes pages.\n\n**Why:** Paging.\n";

interface Daemon {
  items: Item[];
  actions?: boolean;
  readyRuns?: number[];
  projects?: [string, string];
  ticketBody?: string;
  sendBack?: { ok: boolean; detail: string };
}

let client: QueryClient | null = null;

async function mount({ items, actions = false, readyRuns = [9], projects = ["demo", "writer"], ticketBody = "", sendBack = { ok: true, detail: "sent back" } }: Daemon) {
  const statusOf = (project: string): Status => ({ ...served, project, now: NOW, runs: [], queued: [], actions });
  const attention = (project: string, list: Item[]) => ({ level: list.length ? "attention" : "none", now: NOW, project, items: list });
  const [first, second] = projects.map((name) => `/srv/dev/${name}`) as [string, string];
  const fleet = peersFetch(DEMO, {
    [DEMO]: { status: statusOf(first), attention: attention(first, items) },
    [WRITER]: { status: statusOf(second), attention: attention(second, []) },
  });
  const { seen, fetchImpl } = fakeFetch({});
  const routed: typeof fetchImpl = async (url, init) => {
    const merge = url.match(/\/runs\/(\d+)\/merge$/);
    if (merge) return Response.json({ ...ready, run: Number(merge[1]), ready: readyRuns.includes(Number(merge[1])) });
    const ticket = url.match(/\/tickets\/([^/?]+)$/);
    if (ticket) {
      await fetchImpl(url, init);
      return Response.json({ ...ticketDetail, ticket: decodeURIComponent(ticket[1]!), body: ticketBody });
    }
    if (url.endsWith("/actions/send-back")) {
      await fetchImpl(url, init);
      return Response.json(sendBack);
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

function unmount() {
  cleanup();
  client?.clear();
}

const section = () => screen.getByRole("region", { name: "Pull requests" });
const lane = (name: string) => within(section()).getByRole("group", { name });
const rows = () => [...section().querySelectorAll<HTMLElement>("[data-pr-row]")];
const waits = () => rows().map((row) => [
  row.querySelector("[data-pr-wait]")!.textContent,
  row.querySelector("[data-pr-since]")?.textContent ?? "",
]);
const facts = (row: Element) => row.querySelector("[data-facts]")!.textContent;
const rowButton = (pr: number) => within(section()).getByRole("button", { name: new RegExp(`^PR #${pr}, `) });

async function openRow(pr: number) {
  await act(async () => {
    fireEvent.click(rowButton(pr));
    await settle();
  });
  for (let round = 0; round < 2; round += 1) await act(settle);
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  unmount();
  client = null;
  window.location.hash = "";
});

test("a lane per project counts its open pull requests, longest waiting first, and a project with none draws no lane", async () => {
  await mount({ items: [threadPark, readyPark, onCi], projects: ["alpha", "beta"] });
  expect(lane("alpha").querySelector("[data-lane-head]")!.textContent).toContain("3 open");
  expect(within(section()).queryByRole("group", { name: "beta" })).toBeNull();
  expect(section().textContent).not.toContain("0 open");
  expect(rows().map((row) => within(row).getByRole("link", { name: /^PR #/ }).textContent)).toEqual(["PR #41", "PR #42", "PR #43"]);
  unmount();

  await mount({ items: [], projects: ["alpha", "beta"] });
  expect(within(section()).queryAllByRole("group")).toEqual([]);
  expect(section().textContent).toContain("No pull requests open.");
});

test("a row says who it waits on and since when: On CI with its checks, Your review when ready to merge", async () => {
  const shortCi = park(8, "ci", 4 * MINUTE, { number: 41, checks: "pending", review: null, threads: 0, open_threads: 0 });
  await mount({ items: [shortCi, readyPark] });
  expect(waits()).toEqual([["Your review", "ready to merge · 2h"], ["On CI", "checks running · 4m"]]);
});

test("a draft held for review reads Your review over draft and its age, with no chip or repeated state", async () => {
  await mount({ items: [park(12, "pull_request", 2 * 24 * HOUR, {
    state: "draft", title: "Improve the overview", checks: "success", review: "review_required", threads: 0, open_threads: 0,
  })], readyRuns: [] });
  const row = rows()[0]!;
  expect(waits()).toEqual([["Your review", "draft · 2d"]]);
  expect(within(row).queryByText("Draft", { exact: true })).toBeNull();
  expect(row.textContent).not.toContain("held for your review");
  expect(row.textContent).not.toContain("review pending");
});

test("a row names its pull request from its URL and draws its facts line", async () => {
  await mount({ items: [onCi, readyPark, threadPark] });
  const third = rows()[2]!;
  expect(within(third).getByText("PR #43")).toBeTruthy();
  expect(facts(third)).toBe("checks green · 1 open thread");
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

test("an opened row shows its ticket's What line and the links that move it, not the classic detail", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [], ticketBody: WHAT });
  await openRow(42);
  const row = rows()[0]!;
  expect(rowButton(42).getAttribute("aria-expanded")).toBe("true");
  expect(within(row).getByText("Adds a slot that takes pages.")).toBeTruthy();
  expect(within(row).getByRole("link", { name: "Open PR to review" }).getAttribute("href")).toBe(PR_42);
  expect(within(row).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/demo/9");
  expect(within(row).queryByRole("region", { name: "Pull request facts" })).toBeNull();
  expect(row.textContent).not.toContain("Last factory activity");
  expect(within(row).queryByText(PR_42)).toBeNull();
  expect(seen.map((request) => request.url)).toEqual([`${DEMO}/tickets/DEMO-9`]);
});

test("Send back posts the typed note for the run, keeps it on a refusal, and is absent without actions", async () => {
  const row = () => rows()[0]!;
  const sendNote = async () => {
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Send back with a note" }));
      await settle();
    });
    fireEvent.change(within(row()).getByLabelText("Note for the factory"), { target: { value: "Rebase on main first" } });
    await act(async () => {
      fireEvent.click(within(row()).getByRole("button", { name: "Send" }));
      await settle();
    });
  };
  const seen = await mount({ items: [readyPark], actions: true, readyRuns: [] });
  await openRow(42);
  await sendNote();
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map(({ url, body }) => ({ url, body }))).toEqual([{ url: `${DEMO}/actions/send-back`, body: { run: 9, note: "Rebase on main first" } }]);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Sent PR #42 back with your note.");
  unmount();

  await mount({ items: [readyPark], actions: true, readyRuns: [], sendBack: { ok: false, detail: "run 9 is not parked" } });
  await openRow(42);
  await sendNote();
  expect((within(row()).getByLabelText("Note for the factory") as HTMLTextAreaElement).value).toBe("Rebase on main first");
  expect(within(row()).getByRole("alert").textContent).toBe("run 9 is not parked");
  unmount();

  await mount({ items: [readyPark], actions: false, readyRuns: [] });
  await openRow(42);
  expect(within(row()).getByRole("link", { name: "Open run" })).toBeTruthy();
  expect(within(row()).queryByRole("button", { name: "Send back with a note" })).toBeNull();
});

test("the PR number links to GitHub without toggling or fetching the row detail", async () => {
  const seen = await mount({ items: [readyPark], readyRuns: [] });
  const link = within(rows()[0]!).getByRole("link", { name: "PR #42" });
  const button = rowButton(42);
  expect(link.getAttribute("href")).toBe(PR_42);
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noreferrer");
  expect(link.closest("button")).toBeNull();
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(seen).toEqual([]);
  await act(async () => {
    fireEvent.click(button);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(seen.some(({ url }) => url === `${DEMO}/tickets/DEMO-9`)).toBe(true);
  await act(async () => {
    fireEvent.click(link);
    await settle();
  });
  expect(button.getAttribute("aria-expanded")).toBe("true");
});

test("with storage refusing, the tag reopens a folded Needs you and focuses its card", async () => {
  await mount({ items: [blocked, onCi, readyPark, threadPark] });
  const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
  const refuse = () => {
    throw new DOMException("storage disabled", "SecurityError");
  };
  Object.defineProperty(globalThis, "localStorage", { configurable: true, get: refuse });
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
    Object.defineProperty(globalThis, "localStorage", real);
  }
});
