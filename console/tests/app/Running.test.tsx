import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const served = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const served12 = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;

const MINUTE = 60_000;
const NOW = new Date(2026, 9, 15, 12, 0).getTime();
const STALE_MS = served.thresholds.heartbeat_stale_ms;

type Run = Status["runs"][number];

const runOf = (id: number, ticket: string, title: string, fields: Partial<Run>): Run => ({
  ...served.runs[0]!, id, ticket, title, heartbeat_age_ms: 30_000, work_started_ms: null, ...fields,
});
const demoRun = runOf(7, "DEMO-7", "Count the rounds", {
  phase: "reviewing", round: 1, agent_ms: 16 * MINUTE, time_box_ms: 25 * MINUTE,
});
const writerRun = runOf(3, "WRITER-3", "Name the writer", {
  phase: "working", round: 0, agent_ms: 23 * MINUTE, time_box_ms: 25 * MINUTE,
});
function statusOf(project: string, runs: Run[], fields: Partial<Status> = {}): Status {
  return { ...served, project, now: NOW, runs, queued: [], ...fields };
}

/** Each status is one daemon's, the first DEMO's, the second WRITER's; DEMO
 *  answers `/runs/N` from `details`. `answerDemo` makes DEMO's next poll answer
 *  `body` and refetches the fleet. */
async function mount(hash: string, statuses: Status[], details: Record<number, RunDetail> = {}) {
  window.location.hash = hash;
  const bases = [DEMO, WRITER, ...statuses.slice(2).map((_, index) => `http://writer-host:${7712 + index}`)];
  const daemons = Object.fromEntries(
    statuses.map((status, index) => [bases[index]!, { status, attention: { level: "none", now: NOW, project: status.project, items: [] } }]),
  );
  const polls = peersFetch(DEMO, daemons);
  const fetchImpl: Fetch = async (url) => {
    const detail = details[Number(/^\/runs\/(\d+)$/.exec(url.slice(DEMO.length))?.[1])];
    return url.startsWith(`${DEMO}/runs/`) && detail ? Response.json(detail) : polls(url);
  };
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  const client = consoleQueryClient();
  render(
    <QueryClientProvider client={client}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  return {
    answerDemo: async (body: object) => {
      daemons[DEMO] = { ...daemons[DEMO]!, status: body as Status };
      await act(async () => { await client.refetchQueries({ queryKey: ["fleet", DEMO] }); });
      for (let round = 0; round < 4; round += 1) await act(settle);
    },
  };
}

const running = () => screen.getByRole("region", { name: "Running" });
const rows = () => within(running()).queryAllByRole("link");
const rowOf = (ticket: string) => within(running()).getByRole("link", { name: new RegExp(`^${ticket} `) });

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("Running is one list of a row per live run across projects, each with its stage, time and box tone", async () => {
  await mount("#/", [statusOf("/srv/dev/demo", [demoRun]), statusOf("/srv/dev/writer", [writerRun])]);
  const list = within(running()).getByRole("list", { name: "Running runs" });
  expect(within(list).getAllByRole("link").map((row) => row.getAttribute("aria-label"))).toEqual([
    "DEMO-7 Count the rounds, Review · round 2 of 2, 16 of 25 minutes",
    "WRITER-3 Name the writer, Implement, 23 of 25 minutes",
  ]);

  const demo = rowOf("DEMO-7");
  expect(demo.getAttribute("href")).toEndWith("#/runs/demo/7");
  expect(demo.querySelector("[data-stage]")!.textContent).toBe("Review · round 2 of 2");
  expect(within(demo).getByText("16m")).toBeTruthy();
  expect(demo.querySelector("[data-bar]")!.getAttribute("data-bar")).toBe("live");
  expect(rowOf("WRITER-3").querySelector("[data-bar]")!.getAttribute("data-bar")).toBe("need");
});

test("pressing a scoped Running row opens that run's Run page and keeps the scope", async () => {
  const live = runOf(12, "DEMO-12", "Count the rounds", { phase: "reviewing", round: 1 });
  const detail = { ...served12, run: { ...served12.run, id: 12, ticket: "DEMO-12", title: "Count the rounds" } };
  await mount("#/?project=demo", [statusOf("/srv/dev/demo", [live]), statusOf("/srv/dev/writer", [])], { 12: detail });
  await act(async () => {
    fireEvent.click(rowOf("DEMO-12"));
  });
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/runs/demo/12?project=demo");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Count the rounds");
});

test("a run past the heartbeat threshold draws no Running row, and a scoped page draws only its project's runs", async () => {
  const quiet = runOf(9, "DEMO-9", "Went quiet", { heartbeat_age_ms: STALE_MS + MINUTE });
  const demo = statusOf("/srv/dev/demo", [demoRun, quiet]);
  const writer = statusOf("/srv/dev/writer", [writerRun]);
  await mount("#/", [demo, writer]);
  expect(rows().map((row) => row.getAttribute("aria-label")!.split(" ")[0])).toEqual(["DEMO-7", "WRITER-3"]);
  expect(running().querySelector("[data-count]")!.textContent).toBe("2");
  cleanup();

  await mount("#/?project=writer", [demo, writer]);
  expect(rows().map((row) => row.getAttribute("aria-label")!.split(" ")[0])).toEqual(["WRITER-3"]);
});

test("a reviewing run's stage names its round against the cap within it, and its round alone past it", async () => {
  const underCap = runOf(21, "DEMO-21", "Fresh run", { phase: "reviewing", round: 1, max_rounds: 3 });
  const past = runOf(20, "DEMO-20", "Fix the slot", { phase: "reviewing", round: 6, max_rounds: 2 });
  await mount("#/", [statusOf("/srv/dev/demo", [underCap, past], { workers: 2 }), statusOf("/srv/dev/writer", [])]);
  expect(rowOf("DEMO-21").querySelector("[data-stage]")!.textContent).toBe("Review · round 2 of 3");
  expect(rowOf("DEMO-20").querySelector("[data-stage]")!.textContent).toBe("Review · round 7");
  expect(rowOf("DEMO-20").getAttribute("aria-label")).toBe("DEMO-20 Fix the slot, Review · round 7, 0 of 25 minutes");
});

test("a run's activity line names a pending pause or abort without its note", async () => {
  const paused = runOf(30, "DEMO-30", "Pause it", { phase: "merge_gate", stop_requested: "reboot writer", stop_action: "pause", step: "fixing checks (round 1 of 2)" });
  const aborted = runOf(31, "DEMO-31", "Abort it", { phase: "working", stop_requested: "wrong spec", stop_action: "abort", step: null });
  const closed = runOf(32, "DEMO-32", "Close it", { phase: "working", stop_requested: "duplicate", stop_action: "abort_close", step: null });
  await mount("#/", [statusOf("/srv/dev/demo", [paused, aborted, closed], { workers: 3 }), statusOf("/srv/dev/writer", [])]);
  const activity = (ticket: string) => rowOf(ticket).querySelector("[data-activity]")!.textContent;
  expect(activity("DEMO-30")).toBe("Pausing after this turn");
  expect(rowOf("DEMO-30").textContent).not.toContain("reboot writer");
  expect(activity("DEMO-31")).toBe("Aborting");
  expect(activity("DEMO-32")).toBe("Aborting");
});

test("a backticked span in a running row's title shows as inline code, and its accessible name drops the backticks", async () => {
  const live = runOf(7, "DEMO-7", "Read a `+++` TOML header", { phase: "reviewing", round: 1, agent_ms: 16 * MINUTE, time_box_ms: 25 * MINUTE });
  await mount("#/", [statusOf("/srv/dev/demo", [live]), statusOf("/srv/dev/writer", [])]);
  const row = within(running()).getByRole("link", { name: "DEMO-7 Read a +++ TOML header, Review · round 2 of 2, 16 of 25 minutes" });
  expect([...row.querySelectorAll("code")].map((code) => code.textContent)).toEqual(["+++"]);
});

test("a host whose last status is kept beside a contract error draws no Running row", async () => {
  const demo = statusOf("/srv/dev/demo", [demoRun]);
  const page = await mount("#/", [demo, statusOf("/srv/dev/writer", [])]);
  expect(rows()).toHaveLength(1);
  const { queued: _queued, ...unqueued } = demo;
  await page.answerDemo(unqueued);
  expect(rows()).toEqual([]);
  expect(running().textContent).toContain("Nothing running.");
  expect(screen.getByRole("img", { name: "0 running" })).toBeTruthy();
});

test("a running row's activity line is its run's newest event, else its step", async () => {
  const at = (minutes: number, kind: string, summary: string) => ({ ...served12.events[0]!, at: NOW - minutes * MINUTE, kind, summary });
  const events = [at(9, "phase_change", "claimed -> working"), at(2, "agent_tool", "edit holophyte/serve/views.py"), at(5, "phase_change", "working -> verifying")];
  const detail = { ...served12, run: { ...served12.run, id: 7, ticket: "DEMO-7" }, events };
  await mount("#/", [statusOf("/srv/dev/demo", [demoRun]), statusOf("/srv/dev/writer", [{ ...writerRun, step: "fixing round 2" }])], { 7: detail });
  const activity = (ticket: string) => rowOf(ticket).querySelector("[data-activity]")?.textContent;
  expect(activity("DEMO-7")).toBe("edit holophyte/serve/views.py");
  expect(activity("WRITER-3")).toBe("fixing round 2");
});

test("the Running heading's worker dots count busy workers against every worker in scope", async () => {
  await mount("#/", [statusOf("/srv/dev/demo", [demoRun], { workers: 2 }), statusOf("/srv/dev/writer", [writerRun], { workers: 3 })]);
  const dots = within(running()).getByRole("img", { name: "2 of 5 workers busy" });
  expect(dots.getAttribute("title")).toBe("2 of 5 workers busy");
  expect(dots.querySelectorAll("[data-busy]")).toHaveLength(2);
  expect(dots.children).toHaveLength(5);
});

test.each([
  ["working", null, "Implement", ["now", "later", "later", "later", "later"]],
  ["verifying", null, "Verify", ["done", "now", "later", "later", "later"]],
  ["merge_gate", "https://github.com/example/demo/pull/7", "Monitoring PR", ["done", "done", "done", "now", "later"]],
  ["merge_gate", null, "Merge gate", ["done", "done", "done", "now", "later"]],
  ["squashing", null, "Merge", ["done", "done", "done", "done", "now"]],
  ["rebasing", null, "rebasing", ["later", "later", "later", "later", "later"]],
] as const)("a %s run (pull request %s) reads %s and lights its five-step track up to its stage", async (phase, pr_url, label, steps) => {
  await mount("#/", [statusOf("/srv/dev/demo", [runOf(40, "DEMO-40", "Track it", { phase, pr_url })]), statusOf("/srv/dev/writer", [])]);
  const stage = rowOf("DEMO-40").querySelector("[data-stage]")!;
  expect(stage.textContent).toBe(label);
  expect([...stage.querySelectorAll("[data-step]")].map((step) => step.getAttribute("data-step"))).toEqual([...steps]);
});

const witnessesOf = (keys: string[], elapsedMs: number) => ({ story: "DEMO-20", keys, started_ms: NOW - elapsedMs, elapsed_ms: elapsedMs });
const witnessLines = () => [...running().querySelectorAll("[data-witnesses]")].map((line) => line.textContent);

test("a running witness set is a line under Running naming its story, key range and time, even with nothing running", async () => {
  await mount("#/", [statusOf("/srv/dev/demo", [], { witnesses: witnessesOf(["W3", "W4", "W5", "W6"], 4 * MINUTE + 30_000) }), statusOf("/srv/dev/writer", [])]);
  expect(running().textContent).toContain("Nothing running.");
  expect(witnessLines()).toEqual(["Witnesses · DEMO-20 · W3–W6 · running 4m"]);
  cleanup();

  await mount("#/", [statusOf("/srv/dev/demo", [demoRun], { witnesses: witnessesOf(["W1", "W3"], 12 * MINUTE) }), statusOf("/srv/dev/writer", [])]);
  expect(rows()).toHaveLength(1);
  expect(witnessLines()).toEqual(["Witnesses · DEMO-20 · W1, W3 · running 12m"]);
});
