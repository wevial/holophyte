import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Status } from "../../src/lib/contract.gen";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const served = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const MINUTE = 60_000;
const NOW = new Date(2026, 9, 15, 12, 0).getTime();
const STALE_MS = served.thresholds.heartbeat_stale_ms;

type Run = Status["runs"][number];
type Queued = Status["queued"][number];

const runOf = (id: number, ticket: string, title: string, fields: Partial<Run>): Run => ({
  ...served.runs[0]!, id, ticket, title, heartbeat_age_ms: 30_000, work_started_ms: null, ...fields,
});
const demoRun = runOf(7, "DEMO-7", "Count the rounds", {
  phase: "reviewing", round: 1, agent_ms: 16 * MINUTE, time_box_ms: 25 * MINUTE,
});
const writerRun = runOf(3, "WRITER-3", "Name the writer", {
  phase: "working", round: 0, agent_ms: 23 * MINUTE, time_box_ms: 25 * MINUTE,
});
const queuedOf = (ticket: string, fields: Partial<Queued> = {}): Queued => ({
  ticket, ticket_url: null, title: `Ticket ${ticket}`, reason: "ready", pr_url: null, waiting_ms: MINUTE, ...fields,
});

function statusOf(project: string, runs: Run[], fields: Partial<Status> = {}): Status {
  return { ...served, project, now: NOW, runs, queued: [], ...fields };
}

async function mount(hash: string, demo: Status, writer: Status) {
  const attention = (project: string) => ({ level: "none", now: NOW, project, items: [] });
  window.location.hash = hash;
  const fake = fakeDeps(
    peersFetch(DEMO, {
      [DEMO]: { status: demo, attention: attention(demo.project) },
      [WRITER]: { status: writer, attention: attention(writer.project) },
    }),
  );
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const running = () => screen.getByRole("region", { name: "Running" });
const lanes = () => within(running()).queryAllByRole("group").map((lane) => lane.getAttribute("aria-label"));
const lane = (name: string) => within(running()).getByRole("group", { name });
const head = (name: string) => lane(name).querySelector("[data-lane-head]")!.textContent;
const queueButton = (name: string) => within(lane(name)).getByRole("button", { name: /queued$/ });

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a lane per project in record order, each live run a row with its stage, time and box tone", async () => {
  await mount("#/", statusOf("/srv/dev/demo", [demoRun]), statusOf("/srv/dev/writer", [writerRun]));
  expect(lanes()).toEqual(["demo", "writer"]);

  const row = within(lane("demo")).getByRole("link", { name: "DEMO-7 Count the rounds, reviewing, 16 of 25 minutes" });
  expect(row.getAttribute("href")).toBe(`/?classic#run=7&daemon=${encodeURIComponent(DEMO)}`);
  expect(within(row).getByText("reviewing · round 2 of 2")).toBeTruthy();
  expect(within(row).getByText("16m / 25m")).toBeTruthy();
  expect(row.querySelector("[data-bar]")!.getAttribute("data-bar")).toBe("live");

  const writer = within(lane("writer")).getByRole("link", { name: "WRITER-3 Name the writer, implementing, 23 of 25 minutes" });
  expect(writer.querySelector("[data-bar]")!.getAttribute("data-bar")).toBe("need");
});

test("a run past the heartbeat threshold is named as gone quiet, not drawn, and a scoped page draws only its lane", async () => {
  const quiet = { ...writerRun, heartbeat_age_ms: STALE_MS + MINUTE };
  const demo = statusOf("/srv/dev/demo", [demoRun]);
  const writer = statusOf("/srv/dev/writer", [quiet]);
  await mount("#/", demo, writer);
  expect(within(lane("writer")).queryAllByRole("link")).toEqual([]);
  expect(lane("writer").textContent).toContain("WRITER-3 went quiet. It is up in Needs you.");
  expect(head("writer")).toContain("1 of 1 workers busy");
  expect(head("demo")).toContain("1 of 1 workers busy");
  cleanup();

  await mount("#/?project=demo", demo, writer);
  expect(lanes()).toEqual(["demo"]);
});

test("a lane head counts its live and quiet runs against the project's workers, and a chip its round against the cap", async () => {
  const live = [{ ...demoRun, max_rounds: 3 }, runOf(8, "DEMO-8", "Draw the lane", { phase: "working", round: 0 })];
  const quiet = runOf(9, "DEMO-9", "Went quiet", { heartbeat_age_ms: STALE_MS + MINUTE });
  await mount("#/", statusOf("/srv/dev/demo", [...live, quiet], { workers: 3 }), statusOf("/srv/dev/writer", [writerRun]));
  expect(head("demo")).toContain("3 of 3 workers busy");
  expect(within(lane("demo")).getAllByRole("link")).toHaveLength(2);
  const row = within(lane("demo")).getByRole("link", { name: /^DEMO-7 / });
  expect(within(row).getByText("reviewing · round 2 of 3")).toBeTruthy();
});

test("the Running summary sums busy runs and workers over the lanes in scope before the queue", async () => {
  const queued = [queuedOf("DEMO-2"), queuedOf("DEMO-4"), queuedOf("DEMO-5")];
  const demo = statusOf("/srv/dev/demo", [demoRun, runOf(8, "DEMO-8", "Draw the lane", { phase: "working" })], { workers: 3, queued });
  const writer = statusOf("/srv/dev/writer", [writerRun], { workers: 2 });
  await mount("#/", demo, writer);
  const summary = () => running().querySelector("header")!.textContent;
  expect(summary()).toContain("3 of 5 workers busy · 3 queued");
  cleanup();

  await mount("#/?project=writer", demo, writer);
  expect(summary()).toContain("1 of 2 workers busy · nothing queued");
});

test("the queue count unfolds into a row per queued ticket and folds them away again", async () => {
  const queued = [
    queuedOf("DEMO-2", { reason: "approved for merge\nthe next claim resumes it", pr_url: "https://github.com/example/repo/pull/41", waiting_ms: 12 * MINUTE + 4_000 }),
    queuedOf("DEMO-4"),
    queuedOf("DEMO-5"),
  ];
  await mount("#/", statusOf("/srv/dev/demo", [demoRun], { queued }), statusOf("/srv/dev/writer", []));
  const button = queueButton("demo");
  expect(button.textContent).toBe("3 queued");
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(within(lane("demo")).queryByRole("list", { name: "demo queued" })).toBeNull();

  fireEvent.click(button);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  const rows = within(within(lane("demo")).getByRole("list", { name: "demo queued" })).getAllByRole("listitem");
  expect(rows).toHaveLength(3);
  const first = within(rows[0]!);
  const reason = rows[0]!.querySelector("[title]")!;
  expect(reason.getAttribute("title")).toBe("approved for merge\nthe next claim resumes it");
  expect(reason.textContent).toBe("approved for merge");
  expect(first.getByRole("link", { name: "PR #41" })).toBeTruthy();
  expect(first.getByText("waiting 12m 4s")).toBeTruthy();

  fireEvent.click(button);
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(within(lane("demo")).queryByRole("list", { name: "demo queued" })).toBeNull();
});

test("a held project's head carries its hold note and each queued reason ends held", async () => {
  const demo = statusOf("/srv/dev/demo", [demoRun], {
    admission: "held", hold_note: "deploy freeze", queued: [queuedOf("DEMO-2", { reason: "approved" }), queuedOf("DEMO-4")],
  });
  await mount("#/", demo, statusOf("/srv/dev/writer", [writerRun]));
  expect(head("demo")).toContain("held · deploy freeze");
  expect(head("writer")).not.toContain("held");

  fireEvent.click(queueButton("demo"));
  const reasons = within(lane("demo")).getAllByRole("listitem")
    .filter((row) => row.hasAttribute("data-queued"))
    .map((row) => row.querySelector("[title]")!.textContent);
  expect(reasons).toEqual(["approved · held", "ready · held"]);
});

test("with no run live or quiet anywhere, Running draws no lane and keeps its empty sentence", async () => {
  await mount("#/", statusOf("/srv/dev/demo", [], { queued: [queuedOf("DEMO-2")] }), statusOf("/srv/dev/writer", []));
  expect(lanes()).toEqual([]);
  expect(running().textContent).toContain("Nothing running. DEMO-2 is queued.");
});
