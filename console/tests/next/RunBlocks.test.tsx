import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, RunDetail, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();

const PR_88 = "https://github.com/example/repo/pull/88";
const MERGE_SHA = "7b1c0f2e9d8c7b6a5f4e3d2c1b0a99887766554433";
const COMMIT = `https://github.com/example/repo/commit/${MERGE_SHA}`;

const detail = (id: number, ticket: string, facts: Partial<RunDetail["run"]>): RunDetail => ({
  ...servedRun,
  rounds: [],
  run: { ...servedRun.run, id, ticket, title: `${ticket} title`, started_ms: NOW - 2 * 60 * MINUTE, ...facts },
});

const runs: Record<number, RunDetail> = {
  8: detail(8, "DEMO-8", { phase: "awaiting_merge_approval", park_kind: "pull_request", pr_url: PR_88 }),
  9: detail(9, "DEMO-9", {
    phase: "done",
    outcome: "merged",
    pr_url: "https://github.com/example/repo/pull/521",
    merge_sha: MERGE_SHA,
    commit_url: COMMIT,
    ended_ms: new Date(2026, 9, 15, 16, 52).getTime(),
  }),
  12: detail(12, "DEMO-12", { phase: "done", outcome: "merged", pr_url: null, merge_sha: MERGE_SHA, ended_ms: NOW - MINUTE }),
};

const park = {
  kind: "pr_open", level: "attention", run: 8, ticket: "DEMO-8", ticket_url: null, title: "Free stale leases",
  pr_url: PR_88, park_kind: "pull_request", reason: "", asked_ms: NOW - 30 * MINUTE,
  pr: { number: 88, checks: "success", review: "approved", threads: 1, open_threads: 1, title: "Free stale leases" },
} as Attention["items"][number];

const revision = (body: string) => ({ ...servedTicket.current!, body });
const demo8: TicketDetail = {
  ...servedTicket,
  ticket: "DEMO-8",
  body: "## Summary\n\nThe board's body today.\n",
  current: revision("## Summary\n\nThe board's body today.\n"),
  claimed: revision("# Free stale leases\n\n## Summary\n\nA lease past its threshold is freed.\n\nIts run is told before it goes.\n\n## Scope\n\nLeases only.\n"),
};

/** `demo` holding run 8's pull request park, `/runs/N` answering `runs`
 *  and `/tickets/KEY` answering `tickets`, else 404. */
function blocksFetch({ tickets = { "DEMO-8": demo8 }, mergeReady = false }: { tickets?: Record<string, TicketDetail>; mergeReady?: boolean } = {}): Fetch {
  const polls = fleet({ items: [park] }, { items: [] });
  return async (url) => {
    if (!url.startsWith(DEMO)) return polls(url);
    const path = url.slice(DEMO.length);
    if (path === "/runs/8/merge") return Response.json({ ...ready, run: 8, ticket: "DEMO-8", ready: mergeReady });
    const id = /^\/runs\/(\d+)$/.exec(path)?.[1];
    if (id != null) return runs[Number(id)] ? Response.json(runs[Number(id)]) : new Response("not found", { status: 404 });
    const key = /^\/tickets\/([^/?]+)$/.exec(path)?.[1];
    if (key != null) return tickets[key] ? Response.json(tickets[key]) : new Response("not found", { status: 404 });
    return polls(url);
  };
}

async function mount(hash: string, fetch: Fetch = blocksFetch()) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const block = (name: string) => screen.queryByRole("region", { name });
const prLine = () => block("Pull request")!.querySelector("p")!.textContent;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a parked run's pull request line links its number and gives its park's facts, adding ready for your merge once ready", async () => {
  await mount("#/runs/demo/8");
  const pr = block("Pull request")!;
  expect(within(pr).getByRole("link", { name: "PR #88" }).getAttribute("href")).toBe(PR_88);
  expect(prLine()).toBe("PR #88 · checks green · approved · 1 thread open");
  expect([...pr.querySelectorAll("[data-fact]")].map((fact) => fact.getAttribute("data-tone"))).toEqual(["ok", "ok", "warn"]);
  cleanup();
  await mount("#/runs/demo/8", blocksFetch({ mergeReady: true }));
  expect(prLine()).toBe("PR #88 · checks green · approved · 1 thread open · ready for your merge");
});

test("a merged run's line gives its short sha linking to the commit and its time; a run with no pull request has no block", async () => {
  await mount("#/runs/demo/9");
  expect(prLine()).toBe("PR #521 · merged as 7b1c0f2 at 16:52");
  expect(within(block("Pull request")!).getByRole("link", { name: "7b1c0f2" }).getAttribute("href")).toBe(COMMIT);
  cleanup();
  await mount("#/runs/demo/12");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-12 title");
  expect(block("Pull request")).toBeNull();
});

test("the ticket block shows the first paragraph of the claimed revision's summary, and nothing when the ticket answers 404", async () => {
  await mount("#/runs/demo/8");
  const ticket = block("Ticket")!;
  expect(ticket.textContent).toBe("TicketA lease past its threshold is freed.");
  cleanup();
  await mount("#/runs/demo/8", blocksFetch({ tickets: {} }));
  expect(block("Pull request")).not.toBeNull();
  expect(block("Ticket")).toBeNull();
});
