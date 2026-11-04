import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { Attention, MergeQueue, MergeQueueCheck, MergeQueueEntry, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { fakeDeps, peersFetch, settle } from "../harness";

const RELOS = "http://writer-host:7710";
const HOLO = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const served = (await Bun.file(new URL("merge-queue.json", SERVE)).json()) as MergeQueue;

const happyDOM = (window as unknown as { happyDOM: { setViewport: (viewport: { width: number; height?: number }) => void } }).happyDOM;

const MINUTE = 60_000;
const NOW = Date.UTC(2026, 10, 3, 15, 52);
const at = (hour: number, minute: number) => Date.UTC(2026, 10, 3, hour, minute);

const check = (name: string, required: boolean, status: string, conclusion: string | null = null): MergeQueueCheck => ({
  name, workflow: required ? "E2E (localhost)" : "Migration Hygiene", status, conclusion, required,
  started_ms: status === "queued" ? null : NOW - 2 * MINUTE,
  completed_ms: status === "completed" ? NOW - MINUTE : null,
  url: `https://github.com/example/relos/actions/runs/1/job/${name}`,
});

const CHECKS = [
  check("vitest", true, "in_progress"),
  ...["e2e (1/4)", "e2e (2/4)", "e2e (3/4)", "e2e (4/4)", "e2e-ok"].map((name) => check(name, true, "queued")),
  ...["setup", "select-e2e", "changed_prisma_files", "check_migration_hygiene"].map((name) => check(name, false, "completed", "success")),
  check("lint-tests-docs", false, "completed", "skipped"),
  check("build", false, "in_progress"),
  check("scripts", false, "in_progress"),
];

const pull = (number: number, title: string, author = "wevial") => ({ number, title, author, url: `https://github.com/example/relos/pull/${number}`, head_oid: null });

const entry = (position: number, number: number, title: string, added: number, rest: Partial<MergeQueueEntry> = {}): MergeQueueEntry => ({
  position, state: "QUEUED", enqueued_ms: added, estimated_s: position * 600, jump: false, solo: false,
  pr: pull(number, title), group_sha: null, building_ms: null, run: null,
  checks: { done: 0, total: 0, failed: 0, runs: [] }, ...rest,
});

const QUEUE: MergeQueue = {
  ...served,
  now: NOW,
  entries: [
    entry(1, 2547, "[Dependency Update]: Update Vite to 8.3.5", at(15, 41), {
      state: "AWAITING_CHECKS", group_sha: "3f9c2ab0d1e2f3a4", building_ms: at(15, 49),
      checks: { done: 5, total: 13, failed: 0, runs: CHECKS },
    }),
    entry(2, 2549, "[MCP] Add the unit lookup read tool", at(15, 44), { run: { id: 231, ticket: "RELOS-231", phase: "merging", round: 0 } }),
    entry(3, 2550, "[PDF] Keep the page position when a PDF reloads", at(15, 46), { run: { id: 229, ticket: "RELOS-229", phase: "merging", round: 0 } }),
    entry(4, 2552, "[Auth] Stop the sign-in redirect loop on an expired invite", at(15, 50)),
  ],
  left: [
    { pr: pull(2546, "[Admin] Check for an admin in each admin page"), at_ms: at(15, 30), outcome: "merged", reason: null, run: null, waits_on_you: false },
    { pr: pull(2548, "[Admin] Show each brokerage's seat count"), at_ms: at(15, 38), outcome: "removed", reason: "e2e (2/4) failed", run: { id: 227, ticket: "RELOS-227", phase: "addressing", round: 2 }, waits_on_you: false },
    { pr: pull(2544, "[Auth] Keep signed-in users on the page"), at_ms: at(14, 58), outcome: "merged", reason: null, run: { id: 224, ticket: "RELOS-224", phase: "merged", round: 0 }, waits_on_you: false },
  ],
};
const NO_QUEUE: MergeQueue = { ...served, now: NOW, configured: false, configuration: null, entries: [], left: [] };

function queueFetch(relos: MergeQueue, holo: MergeQueue = NO_QUEUE): Fetch {
  const daemon = (project: string) => ({
    status: { ...working, host: "writer-host", project },
    attention: { level: "none", now: NOW, project, items: [] } as Attention,
  });
  const polls = peersFetch(RELOS, { [RELOS]: daemon("/srv/dev/relos"), [HOLO]: daemon("/srv/dev/holophyte") });
  return async (url, init) => {
    if (url === `${RELOS}/merge-queue`) return Response.json(relos);
    if (url === `${HOLO}/merge-queue`) return Response.json(holo);
    return polls(url, init);
  };
}

async function mount(hash: string, fetch: Fetch, width = 1440) {
  happyDOM.setViewport({ width, height: 900 });
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={RELOS} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

afterEach(() => {
  cleanup();
  window.location.hash = "";
  happyDOM.setViewport({ width: 1024, height: 768 });
});

const countLine = () => document.querySelector("[data-count-line]")!.textContent;
const rows = () => [...screen.getByRole("list", { name: "relos merge queue" }).querySelectorAll<HTMLElement>("[data-entry]")];

test("the page heads the project's queue with its counts and settings and lists its entries in position order", async () => {
  await mount("#/merge-queue?project=relos", queueFetch(QUEUE));
  expect(countLine()).toMatch(/^relos · main · 4 queued \/ 1 building \/ clears ~\d\d:\d\d$/);
  const tab = document.querySelector("[data-queue-table] [data-lane-head]")!.textContent!;
  for (const words of ["squash", "all green", "builds 1 at a time", "60m"]) expect(tab).toContain(words);
  expect(rows().map((row) => row.getAttribute("data-entry"))).toEqual(["1", "2", "3", "4"]);
  const [first, second, , fourth] = rows();
  expect(first!.querySelector("[data-entry-state]")!.textContent).toBe("checks running");
  expect(first!.textContent).toContain("5/13 done");
  expect(within(second!).getByRole("link", { name: "RELOS-231" }).getAttribute("href")).toMatch(/#\/tickets\/relos\/RELOS-231(\?|$)/);
  expect(second!.querySelector("[data-entry-state]")!.textContent).toBe("queued");
  expect(fourth!.querySelector("[data-entry-sub]")!.textContent).toBe("wevial");
});

test("a row's chevron opens its merge group and its checks split into required and other, and closes them again", async () => {
  await mount("#/merge-queue?project=relos", queueFetch(QUEUE));
  const toggle = within(rows()[0]!).getByRole("button", { name: /^Position 1/ });
  await act(async () => void fireEvent.click(toggle));
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const detail = rows()[0]!.querySelector("[data-entry-detail]") as HTMLElement;
  expect(detail.textContent).toContain("merge group 3f9c2ab ·");
  const required = within(detail).getByRole("region", { name: "Required" });
  const other = within(detail).getByRole("region", { name: "Other" });
  expect(required.querySelector("[data-check-count]")!.textContent).toBe("0 of 6 done");
  expect(other.querySelector("[data-check-count]")!.textContent).toBe("5 of 7 done");
  const names = (column: HTMLElement) => within(column).getAllByRole("link").map((link) => link.textContent);
  expect(names(required)).toEqual(CHECKS.filter((each) => each.required).map((each) => each.name));
  for (const link of within(detail).getAllByRole("link").filter((each) => each.closest("[data-check]"))) {
    expect(link.getAttribute("href")).toBe(CHECKS.find((each) => each.name === link.textContent)!.url);
  }

  await act(async () => void fireEvent.click(toggle));
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(rows()[0]!.querySelector("[data-entry-detail]")).toBeNull();
});

test("Left the queue lists the removals and merges newest first with their clock times, and is absent when nothing left", async () => {
  await mount("#/merge-queue?project=relos", queueFetch(QUEUE));
  const left = screen.getByRole("region", { name: "relos left the queue" });
  const read = [...left.querySelectorAll("[data-left]")].map((row) => [
    row.querySelector("[data-left-outcome]")!.textContent, row.querySelector("[data-left-at]")!.textContent,
  ]);
  expect(read).toEqual([["removed: e2e (2/4) failed", "15:38"], ["merged", "15:30"], ["merged", "14:58"]]);
  cleanup();

  await mount("#/merge-queue?project=relos", queueFetch({ ...QUEUE, left: [] }));
  expect(rows()).toHaveLength(4);
  expect(screen.queryByText("Left the queue")).toBeNull();
});

test("a project without a merge queue reads one panel and no table, and All projects names it on one quiet line", async () => {
  await mount("#/merge-queue?project=holophyte", queueFetch(QUEUE));
  expect(countLine()).toBe("holophyte · main");
  expect(screen.getByText("No merge queue on this repository.")).toBeTruthy();
  expect(document.querySelector("[data-queue-table]")).toBeNull();
  cleanup();

  await mount("#/merge-queue", queueFetch(QUEUE));
  expect([...document.querySelectorAll("[data-queue-lane]")].map((lane) => lane.getAttribute("aria-label"))).toEqual(["relos"]);
  expect(document.querySelector("[data-no-queue]")!.textContent).toBe("No merge queue on holophyte.");
});

test("at 390px the rows drop the column head and carry the PR number on the title's second line", async () => {
  await mount("#/merge-queue?project=relos", queueFetch(QUEUE), 390);
  expect(document.querySelector("[data-queue-heads]")).toBeNull();
  expect(rows().map((row) => row.querySelector("[data-entry-sub]")!.textContent)).toEqual([
    "#2547 · wevial", "#2549 · RELOS-231 · merging", "#2550 · RELOS-229 · merging", "#2552 · wevial",
  ]);
});
