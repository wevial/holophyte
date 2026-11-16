import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, RunDetail, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
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
  pr: { number: 88, checks: "success", review: "approved", threads: 1, open_threads: 1, title: "Free stale leases", state: "ready" },
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
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const block = (name: string) => screen.queryByRole("region", { name });
const prFact = () => screen.getByRole("img", { name: "Pull request" }).closest("a")!;
const ticketFold = () => document.querySelector<HTMLDetailsElement>("[data-run-ticket]");
const ticketLead = () => ticketFold()!.querySelector("summary")!.nextElementSibling!.textContent;

async function prTip(): Promise<string | null | undefined> {
  await act(async () => {
    fireEvent.keyDown(document.body, { key: "Tab" });
    prFact().focus();
  });
  return screen.queryByRole("tooltip")?.textContent;
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a parked run's Pull request fact links its number and its tooltip gives its park's facts, adding ready for your merge once ready", async () => {
  await mount("#/runs/demo/8");
  expect(prFact().textContent).toBe("PR #88");
  expect(prFact().getAttribute("href")).toBe(PR_88);
  expect(await prTip()).toBe("checks green · approved · 1 thread open");
  expect(block("Pull request")).toBeNull();
  cleanup();
  await mount("#/runs/demo/8", blocksFetch({ mergeReady: true }));
  expect(await prTip()).toBe("checks green · approved · 1 thread open · ready for your merge");
  expect(screen.getByRole("img", { name: "Pull request" }).getAttribute("class")).toContain("text-live");
});

test("a merged run's Pull request fact names its sha and time, the verdict's short sha links to the commit; a run with no pull request has no fact", async () => {
  await mount("#/runs/demo/9");
  expect(await prTip()).toBe("merged as 7b1c0f2 at 16:52");
  const verdict = within(screen.getByRole("main")).getByRole("status");
  expect(verdict.textContent).toBe("Merged to main as 7b1c0f2 at 16:52.");
  expect(within(verdict).getByRole("link", { name: "7b1c0f2" }).getAttribute("href")).toBe(COMMIT);
  cleanup();
  await mount("#/runs/demo/12");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-12 title");
  expect(screen.queryByRole("img", { name: "Pull request" })).toBeNull();
});

test("the Ticket fold opens from closed to the first paragraph of the claimed revision's summary, and is absent when the ticket answers 404", async () => {
  await mount("#/runs/demo/8");
  const fold = ticketFold()!;
  expect(fold.open).toBe(false);
  const summary = fold.querySelector("summary")!;
  expect(summary.textContent).toBe("Ticket");
  await act(async () => void fireEvent.click(summary));
  expect(fold.open).toBe(true);
  expect(ticketLead()).toBe("A lease past its threshold is freed.");
  cleanup();
  await mount("#/runs/demo/8", blocksFetch({ tickets: {} }));
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-8 title");
  expect(ticketFold()).toBeNull();
});

async function go(hash: string) {
  await act(async () => {
    window.location.hash = hash;
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  for (let round = 0; round < 6; round += 1) await act(settle);
}

test("a Ticket fold opened on one run is closed on returning to another run already loaded", async () => {
  await mount("#/runs/demo/9", blocksFetch({ tickets: { "DEMO-8": demo8, "DEMO-9": { ...demo8, ticket: "DEMO-9" } } }));
  await go("#/runs/demo/8");
  await act(async () => void fireEvent.click(ticketFold()!.querySelector("summary")!));
  expect(ticketFold()!.open).toBe(true);
  await go("#/runs/demo/9");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-9 title");
  expect(ticketFold()!.open).toBe(false);
});

test("a ticket claimed as an older snapshot shows its current body's first paragraph, prose directly under its title included", async () => {
  const snapshot: TicketDetail = {
    ...demo8,
    claimed: { revision: null, title: "Free stale leases", acceptance_criteria: [], verification_commands: [] },
    current: revision("# Free stale leases\nA lease past its threshold is freed.\n\nIts run is told before it goes.\n"),
  };
  await mount("#/runs/demo/8", blocksFetch({ tickets: { "DEMO-8": snapshot } }));
  expect(ticketLead()).toBe("A lease past its threshold is freed.");
});

test("a heading directly under the first paragraph ends it, so the next section's prose stays out of the ticket block", async () => {
  const sections: TicketDetail = {
    ...demo8,
    claimed: revision("# Free stale leases\nA lease past its threshold is freed.\n## Acceptance\nIts run is told before it goes.\n"),
  };
  await mount("#/runs/demo/8", blocksFetch({ tickets: { "DEMO-8": sections } }));
  expect(ticketLead()).toBe("A lease past its threshold is freed.");
});

test("an empty summary draws no ticket block rather than another section's prose", async () => {
  const empty: TicketDetail = {
    ...demo8,
    claimed: revision("# Free stale leases\n\n## Summary\n\n## Acceptance criteria\n\nThe operator can retry.\n"),
  };
  await mount("#/runs/demo/8", blocksFetch({ tickets: { "DEMO-8": empty } }));
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-8 title");
  expect(ticketFold()).toBeNull();
});
