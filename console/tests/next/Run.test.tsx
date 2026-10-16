import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const changes = servedRun.rounds.find((round) => round.verdict === "changes_requested")!;

const STARTED = new Date(2026, 9, 15, 11, 30).getTime();
const detail = (id: number, ticket: string, facts: Partial<RunDetail["run"]>, rounds: RunDetail["rounds"] = []): RunDetail => ({
  ...servedRun,
  rounds,
  run: { ...servedRun.run, id, ticket, title: `${ticket} title`, host: "writer-1", started_ms: STARTED, ...facts },
});

const runs: Record<number, RunDetail> = {
  12: detail(12, "DEMO-12", { title: "Count the rounds", phase: "reviewing", agent_ms: 16 * MINUTE, time_box_ms: 25 * MINUTE }, [changes]),
  40: detail(40, "DEMO-40", {
    phase: "failed",
    outcome: "failed",
    failure_kind: "verify",
    outcome_reason: "2 tests red in tests/test_board_cache.py\nmore",
    ended_ms: NOW - 10 * MINUTE,
  }),
  9: detail(9, "DEMO-9", {
    phase: "done",
    outcome: "merged",
    merge_sha: "5acc1384b2e0f6a7c9d8e1f203a4b5c6d7e8f901",
    ended_ms: new Date(2026, 9, 15, 11, 15).getTime(),
  }),
};

const failedItem = {
  kind: "failed", level: "attention", run: 40, ticket: "DEMO-40", ticket_url: null, reason: "verify failed",
  ended_ms: NOW - 10 * MINUTE, attempt: 1, pr_url: null, triage: null,
} as Attention["items"][number];

const liveRow = {
  ...run(12, "DEMO-12"),
  title: "Count the rounds",
  phase: "reviewing",
  round: 1,
  max_rounds: 3,
  agent_ms: 16 * MINUTE,
  time_box_ms: 25 * MINUTE,
  work_started_ms: null,
};

/** DEMO is `demo`, live in run 12 and holding a failed attention item for
 *  run 40; `/runs/N` answers `runs`, else 404. */
function runFetch(live = liveRow): Fetch {
  const polls = fleet({ items: [failedItem], runs: [live] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/status`) {
      const status = (await (await polls(url)).json()) as Status;
      return Response.json({ ...status, route_labels: { ...status.route_labels, implementer: "claude opus", reviewer: "codex gpt-6-sol" } });
    }
    const id = /^\/runs\/(\d+)$/.exec(url.slice(DEMO.length))?.[1];
    if (url.startsWith(DEMO) && id != null) {
      const answer = runs[Number(id)];
      return answer ? Response.json(answer) : new Response("not found", { status: 404 });
    }
    return polls(url);
  };
}

async function mount(hash: string, fetch: Fetch = runFetch()) {
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

const verdict = () => within(screen.getByRole("main")).getByRole("status").textContent;
const stage = () => screen.getByRole("group", { name: "Stage" }).textContent;

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

test("a reviewing run's header names its ticket, host, box and routes, says where it stands and counts its loop back", async () => {
  await mount("#/runs/demo/12");
  const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
  expect(crumb.textContent).toBe("Runs / demo tickets / DEMO-12 / run 12");
  expect(within(crumb).getByRole("link", { name: "Runs" }).getAttribute("href")).toEndWith("#/runs");
  expect(within(crumb).getByRole("link", { name: "demo tickets" }).getAttribute("href")).toEndWith("#/tickets?project=demo");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Count the rounds");
  expect(document.querySelector("[data-run-facts]")!.textContent).toBe(
    "DEMO-12 · run 12 · demo on writer-1 · started 11:30 PDT · 16m of a 25m box · implementer claude opus · reviewer codex gpt-6-sol",
  );
  expect(verdict()).toBe("Reviewing round 2 of 3. Nothing needs you.");
  expect(stage()).toBe("Review · round 2 of 3 · 1 loop back");
});

test("a failed run waiting in Needs you gives its failure and reason, and a merged run its sha and time", async () => {
  await mount("#/runs/demo/40");
  expect(verdict()).toBe("Failed at verify: 2 tests red in tests/test_board_cache.py.");
  expect(stage()).toBe("Failed at verify");
  cleanup();
  await mount("#/runs/demo/9");
  expect(verdict()).toBe("Merged to main as 5acc138 at 11:15. Nothing needs you.");
  expect(stage()).toBe("Merged · 5acc138");
});

test("a run past its heartbeat threshold says how long it has been silent, and that alone, before its daemon raises it", async () => {
  await mount("#/runs/demo/12", runFetch({ ...liveRow, heartbeat_age_ms: 15 * MINUTE }));
  expect(verdict()).toBe("No heartbeat for 15m. It is up in Needs you.");
  expect(stage()).toBe("Stale · no beat 15m");
});

test("an unknown project, a run the daemon lacks and a host yet to answer each say so", async () => {
  await mount("#/runs/nowhere/12");
  expect(screen.getByText("No project named nowhere answers this console.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Back to Runs" }).getAttribute("href")).toEndWith("#/runs");
  cleanup();
  await mount("#/runs/demo/999");
  expect(screen.getByText("demo has no run 999.")).toBeTruthy();
  cleanup();
  await mount("#/runs/demo/12", () => new Promise<Response>(() => {}));
  expect(verdict()).toBe("Asking demo for run 12…");
});

test("a run page under another project's scope shows its own project's run and keeps the scope", async () => {
  await mount("#/runs/demo/12?project=other");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Count the rounds");
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("project")).toBe("other");
});
