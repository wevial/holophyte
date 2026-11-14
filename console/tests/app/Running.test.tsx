import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { captureIntervals, fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const WRITER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const served = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const served12 = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;

const MINUTE = 60_000;
const NOW = new Date(2026, 9, 15, 12, 0).getTime();
const STALE_MS = served.thresholds.heartbeat_stale_ms;

type Run = Status["runs"][number];
type Item = Attention["items"][number];

const runOf = (id: number, ticket: string, title: string, fields: Partial<Run>): Run => ({
  ...served.runs[0]!, id, ticket, title, heartbeat_age_ms: 30_000, work_started_ms: null, ...fields,
});
const demoRun = runOf(7, "DEMO-7", "Count the rounds", {
  phase: "reviewing", round: 1, agent_ms: 16 * MINUTE, time_box_ms: 25 * MINUTE,
});
const writerRun = runOf(3, "WRITER-3", "Name the writer", {
  phase: "working", round: 0, agent_ms: 23 * MINUTE, time_box_ms: 25 * MINUTE,
});
const staleItem = ({ id, ticket, phase }: Run): Item => ({
  kind: "stale_run", level: "attention", run: id, ticket, ticket_url: null, phase, heartbeat_age_ms: STALE_MS + MINUTE, pr_url: null,
});
function statusOf(project: string, runs: Run[], fields: Partial<Status> = {}): Status {
  return { ...served, project, now: NOW, runs, queued: [], ...fields };
}

/** Each status is one daemon's, the first DEMO's, the second WRITER's, and its
 *  attention lists `listed` at the same place; DEMO answers `/runs/N` from `details`.
 *  `answerDemo` makes DEMO's next poll answer `body` and `items`, and refetches. */
async function mount(hash: string, statuses: Status[], details: Record<number, RunDetail> = {}, listed: Item[][] = []) {
  window.location.hash = hash;
  const bases = [DEMO, WRITER, ...statuses.slice(2).map((_, index) => `http://writer-host:${7712 + index}`)];
  const daemons = Object.fromEntries(
    statuses.map((status, index) => [bases[index]!, { status, attention: { level: "none", now: NOW, project: status.project, items: listed[index] ?? [] } }]),
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
    clock: fake.clock,
    answerDemo: async (body: object, items: Item[] = []) => {
      daemons[DEMO] = { status: body as Status, attention: { level: "none", now: (body as Status).now, project: (body as Status).project, items } };
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
  await mount("#/", [demo, writer], {}, [[staleItem(quiet)]]);
  expect(rows().map((row) => row.getAttribute("aria-label")!.split(" ")[0])).toEqual(["DEMO-7", "WRITER-3"]);
  expect(running().querySelector("[data-count]")!.textContent).toBe("2");
  cleanup();

  await mount("#/?project=writer", [demo, writer], {}, [[staleItem(quiet)]]);
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

test("a run that goes quiet on the console's clock keeps its Running row until its daemon lists it under Needs you", async () => {
  const intervals = captureIntervals();
  try {
    const page = await mount("#/", [statusOf("/srv/dev/demo", [demoRun])]);
    page.clock.now = NOW + STALE_MS;
    act(intervals.fire);
    expect(rows().map((row) => row.getAttribute("aria-label")!.split(" ")[0])).toEqual(["DEMO-7"]);
    const quiet = { ...demoRun, heartbeat_age_ms: STALE_MS + 30_000 };
    await page.answerDemo(statusOf("/srv/dev/demo", [quiet], { now: NOW + STALE_MS }), [staleItem(quiet)]);
    expect(rows()).toEqual([]);
    expect(within(screen.getByRole("region", { name: "Needs you" })).getByText("DEMO-7")).toBeTruthy();
  } finally {
    intervals.restore();
  }
});

const dotsLabel = () => within(running()).getByRole("img", { name: /workers busy$/ }).getAttribute("aria-label");
const tickets = () => rows().map((row) => row.getAttribute("aria-label")!.split(" ")[0]);

test("a run gone quiet draws no row but still holds a busy worker in the heading's dots", async () => {
  const quiet = runOf(9, "DEMO-9", "Went quiet", { heartbeat_age_ms: STALE_MS + MINUTE });
  await mount("#/", [statusOf("/srv/dev/demo", [demoRun, quiet], { workers: 2 }), statusOf("/srv/dev/writer", [], { workers: 1 })], {}, [[staleItem(quiet)]]);
  expect(tickets()).toEqual(["DEMO-7"]);
  expect(dotsLabel()).toBe("2 of 3 workers busy");
});

test("a scoped page's worker dots count only the workers and runs of the project in scope", async () => {
  const demo = statusOf("/srv/dev/demo", [demoRun, runOf(8, "DEMO-8", "Draw the row", { phase: "working" })], { workers: 3 });
  await mount("#/?project=writer", [demo, statusOf("/srv/dev/writer", [writerRun], { workers: 2 })]);
  expect(dotsLabel()).toBe("1 of 2 workers busy");
});

test("more live runs than workers light every dot and add none", async () => {
  const live = [demoRun, runOf(8, "DEMO-8", "Second", { phase: "working" }), runOf(9, "DEMO-9", "Third", { phase: "verifying" })];
  await mount("#/", [statusOf("/srv/dev/demo", live, { workers: 1 }), statusOf("/srv/dev/writer", [], { workers: 1 })]);
  expect(rows()).toHaveLength(3);
  expect(dotsLabel()).toBe("2 of 2 workers busy");
  expect(within(running()).getByRole("img", { name: /workers busy$/ }).children).toHaveLength(2);
});

test("an idle project adds its workers to the dots and no row", async () => {
  const alpha = statusOf("/srv/dev/alpha", [demoRun], { workers: 2 });
  const beta = statusOf("/srv/dev/beta", [], { workers: 2 });
  const gamma = statusOf("/srv/dev/gamma", [], { workers: 2 });
  await mount("#/", [alpha, beta, gamma]);
  expect(tickets()).toEqual(["DEMO-7"]);
  expect(dotsLabel()).toBe("1 of 6 workers busy");
});

test("with every project idle, Running draws no row, counts none and says Nothing running.", async () => {
  await mount("#/", [statusOf("/srv/dev/alpha", []), statusOf("/srv/dev/beta", []), statusOf("/srv/dev/gamma", [])]);
  expect(rows()).toEqual([]);
  expect(running().querySelector("[data-count]")!.textContent).toBe("0");
  expect(running().textContent).toContain("Nothing running.");
  expect(dotsLabel()).toBe("0 of 3 workers busy");
});

test("a host kept beside a contract error adds no workers to the dots", async () => {
  const demo = statusOf("/srv/dev/demo", [demoRun], { workers: 2 });
  const page = await mount("#/", [demo, statusOf("/srv/dev/writer", [writerRun], { workers: 1 })]);
  expect(dotsLabel()).toBe("2 of 3 workers busy");
  const { queued: _queued, ...unqueued } = demo;
  await page.answerDemo(unqueued);
  expect(tickets()).toEqual(["WRITER-3"]);
  expect(dotsLabel()).toBe("1 of 1 workers busy");
});

test("a running witness set holds no worker busy", async () => {
  await mount("#/", [statusOf("/srv/dev/demo", [], { workers: 2, witnesses: witnessesOf(["W1", "W2"], MINUTE) }), statusOf("/srv/dev/writer", [], { workers: 1 })]);
  expect(witnessLines()).toHaveLength(1);
  expect(dotsLabel()).toBe("0 of 3 workers busy");
});

test("with no witness set running, a live run draws no witness line", async () => {
  await mount("#/", [statusOf("/srv/dev/demo", [demoRun], { witnesses: null }), statusOf("/srv/dev/writer", [writerRun], { witnesses: null })]);
  expect(rows()).toHaveLength(2);
  expect(witnessLines()).toEqual([]);
});

test("a check-fix round's multi-line step shows only its first line in the activity line", async () => {
  const fixing = runOf(30, "DEMO-30", "Fix the checks", { phase: "merge_gate", step: "fixing checks (round 1 of 2)\nruff check . failed" });
  await mount("#/", [statusOf("/srv/dev/demo", []), statusOf("/srv/dev/writer", [fixing])]);
  expect(rowOf("DEMO-30").querySelector("[data-activity]")!.textContent).toBe("fixing checks (round 1 of 2)");
});

test("a run with no event, step or stop draws no activity line under its title", async () => {
  await mount("#/", [statusOf("/srv/dev/demo", []), statusOf("/srv/dev/writer", [{ ...writerRun, step: null }])]);
  expect(rowOf("WRITER-3").querySelector("[data-activity]")).toBeNull();
  expect(within(rowOf("WRITER-3")).getByText("Name the writer")).toBeTruthy();
});

test("a run with no time box shows its minutes without a bar, and its name says the minutes alone", async () => {
  const unboxed = runOf(50, "DEMO-50", "No box", { phase: "working", agent_ms: 7 * MINUTE, time_box_ms: null });
  await mount("#/", [statusOf("/srv/dev/demo", [unboxed]), statusOf("/srv/dev/writer", [])]);
  const row = rowOf("DEMO-50");
  expect(row.getAttribute("aria-label")).toBe("DEMO-50 No box, Implement, 7 minutes");
  expect(within(row).getByText("7m")).toBeTruthy();
  expect(row.querySelector("[data-bar]")).toBeNull();
});

test("a run with no agent time yet shows no time or bar, and its name stops at its stage", async () => {
  const fresh = runOf(51, "DEMO-51", "Just claimed", { phase: "working", agent_ms: null, working_ms: null, time_box_ms: 25 * MINUTE });
  await mount("#/", [statusOf("/srv/dev/demo", [fresh]), statusOf("/srv/dev/writer", [])]);
  const row = rowOf("DEMO-51");
  expect(row.getAttribute("aria-label")).toBe("DEMO-51 Just claimed, Implement");
  expect(row.querySelector("[data-bar]")).toBeNull();
  expect(row.textContent).not.toMatch(/\d+[ms]$/);
});

test("the Running list is a size container whose every row layout keeps a shrinkable title column", async () => {
  await mount("#/", [statusOf("/srv/dev/demo", [demoRun]), statusOf("/srv/dev/writer", [])]);
  const list = within(running()).getByRole("list", { name: "Running runs" });
  expect(list.classList.contains("@container")).toBe(true);
  const layouts = [...rowOf("DEMO-7").classList].filter((name) => name.includes("grid-cols-"));
  expect(layouts.length).toBeGreaterThan(1);
  expect(layouts.filter((name) => /(^|:)(min|max)-\[|^(sm|md|lg|xl|2xl):/.test(name))).toEqual([]);
  expect(layouts.every((name) => name.includes("minmax(0,1fr)"))).toBe(true);
});
