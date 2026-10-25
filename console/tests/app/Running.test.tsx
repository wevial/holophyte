import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, RunDetail, Status } from "../../src/lib/contract.gen";
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
type Queued = Status["queued"][number];
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
const queuedOf = (ticket: string, fields: Partial<Queued> = {}): Queued => ({
  ticket, ticket_url: null, title: `Ticket ${ticket}`, reason: "ready", pr_url: null, waiting_ms: MINUTE, ...fields,
});

const prPark = (run: number, parkKind: string, ageMs: number, pr: object): Item => ({
  kind: "pr_open", level: "attention", run, ticket: `DEMO-${run}`, ticket_url: null, title: `Ticket ${run}`,
  park_kind: parkKind, reason: "PR open", asked_ms: NOW - ageMs,
  pr_url: `https://github.com/example/demo/pull/${run + 33}`, pr: { title: null, state: "ready", number: run + 33, threads: 0, open_threads: 0, ...pr },
} as Item);
const draftPark = prPark(12, "pull_request", 3 * MINUTE, { state: "draft", checks: "success", review: "review_required" });
const readyPark = prPark(9, "pull_request", 2 * MINUTE, { checks: "success", review: "approved" });
const ciPark = prPark(8, "ci", 4 * MINUTE, { checks: "pending", review: null });

function statusOf(project: string, runs: Run[], fields: Partial<Status> = {}): Status {
  return { ...served, project, now: NOW, runs, queued: [], ...fields };
}

/** Both daemons answer their polls; DEMO answers `/runs/N` from `details`. */
async function mount(hash: string, demo: Status, writer: Status, details: Record<number, RunDetail> = {}) {
  await mountAll(hash, [demo, writer], details);
}

/** The first status is DEMO's, the second WRITER's, each later one a further
 *  daemon's; `items` are DEMO's attention items. `answerDemo` makes DEMO's
 *  next poll answer `body` and refetches the fleet. */
async function mountAll(hash: string, statuses: Status[], details: Record<number, RunDetail> = {}, items: Item[] = []) {
  const attention = (project: string, own: Item[]) => ({ level: own.length ? "attention" : "none", now: NOW, project, items: own });
  window.location.hash = hash;
  const bases = [DEMO, WRITER, ...statuses.slice(2).map((_, index) => `http://writer-host:${7712 + index}`)];
  const daemons = Object.fromEntries(
    statuses.map((status, index) => [bases[index]!, { status, attention: attention(status.project, index === 0 ? items : []) }]),
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
const lanes = () => within(running()).queryAllByRole("group").map((lane) => lane.getAttribute("aria-label"));
const lane = (name: string) => within(running()).getByRole("group", { name });
const head = (name: string) => lane(name).querySelector("[data-lane-head]")!.textContent;
const queueButton = (name: string) => within(lane(name)).getByRole("button", { name: /queued$/ });

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a lane per project in record order, each live run a row with its stage, time and box tone", async () => {
  await mount("#/", statusOf("/srv/dev/demo", [demoRun]), statusOf("/srv/dev/writer", [writerRun]));
  expect(lanes()).toEqual(["demo", "writer"]);

  const row = within(lane("demo")).getByRole("link", { name: "DEMO-7 Count the rounds, reviewing, round 2 of 2, 16 of 25 minutes" });
  expect(row.getAttribute("href")).toEndWith("#/runs/demo/7");
  expect(within(row).getByText("reviewing · round 2 of 2")).toBeTruthy();
  expect(within(row).getByText("16m / 25m")).toBeTruthy();
  expect(row.querySelector("[data-bar]")!.getAttribute("data-bar")).toBe("live");

  const writer = within(lane("writer")).getByRole("link", { name: "WRITER-3 Name the writer, implementing, 23 of 25 minutes" });
  expect(writer.querySelector("[data-bar]")!.getAttribute("data-bar")).toBe("need");
});

test("pressing a scoped Running row opens that run's Run page and keeps the scope", async () => {
  const live = runOf(12, "DEMO-12", "Count the rounds", { phase: "reviewing", round: 1 });
  const detail = { ...served12, run: { ...served12.run, id: 12, ticket: "DEMO-12", title: "Count the rounds" } };
  await mount("#/?project=demo", statusOf("/srv/dev/demo", [live]), statusOf("/srv/dev/writer", []), { 12: detail });
  await act(async () => {
    fireEvent.click(within(lane("demo")).getByRole("link", { name: /^DEMO-12 / }));
  });
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/runs/demo/12?project=demo");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Count the rounds");
});

test("a run past the heartbeat threshold is named as gone quiet, not drawn, and a scoped page draws only its lane", async () => {
  const quiet = { ...writerRun, heartbeat_age_ms: STALE_MS + MINUTE };
  const demo = statusOf("/srv/dev/demo", [demoRun]);
  const writer = statusOf("/srv/dev/writer", [quiet]);
  await mount("#/", demo, writer);
  expect(within(lane("writer")).queryAllByRole("link")).toEqual([]);
  expect(lane("writer").textContent).toContain("WRITER-3 went quiet. It is up in Waiting on you.");
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

test("with no run anywhere, a project with a queued ticket keeps its lane and an idle one draws none", async () => {
  await mount("#/", statusOf("/srv/dev/demo", [], { queued: [queuedOf("DEMO-2")] }), statusOf("/srv/dev/writer", []));
  expect(lanes()).toEqual(["demo"]);
  expect(queueButton("demo").textContent).toBe("1 queued");
  expect(head("demo")).toContain("· Nothing running.");
  expect(within(lane("demo")).queryByRole("list", { name: "demo running" })).toBeNull();
  expect(running().querySelector("header")!.textContent).toEndWith("1 queued");
});

test("a host whose last status is kept beside a contract error draws no lane, and Running neither names nor counts its queue", async () => {
  const demo = statusOf("/srv/dev/demo", [], { queued: [queuedOf("DEMO-2")] });
  const page = await mountAll("#/", [demo, statusOf("/srv/dev/writer", [])]);
  expect(lanes()).toEqual(["demo"]);
  const { queued: _queued, ...unqueued } = demo;
  await page.answerDemo(unqueued);
  expect(lanes()).toEqual([]);
  expect(running().textContent).toContain("Nothing running and nothing queued.");
  expect(running().textContent).not.toContain("DEMO-2");
  expect(running().querySelector("header")!.textContent).toEndWith("nothing queued");
});

test("a lane is drawn only for a project with a run or a queued ticket, while the summary still counts every worker", async () => {
  const alpha = statusOf("/srv/dev/alpha", [demoRun], { workers: 2 });
  const beta = statusOf("/srv/dev/beta", [], { workers: 2 });
  const gamma = statusOf("/srv/dev/gamma", [], { workers: 2, queued: [queuedOf("GAMMA-1")] });
  await mountAll("#/", [alpha, beta, gamma]);
  expect(lanes()).toEqual(["alpha", "gamma"]);
  expect(queueButton("gamma").textContent).toBe("1 queued");
  expect(running().textContent).not.toContain("beta");
  expect(running().querySelector("header")!.textContent).toContain("1 of 6 workers busy · 1 queued");
});

test("a held project with nothing running or queued keeps its lane and held tag when no project is running", async () => {
  const beta = statusOf("/srv/dev/beta", []);
  const delta = statusOf("/srv/dev/delta", [], { admission: "held", hold_note: "deploy freeze" });
  await mountAll("#/", [beta, delta]);
  expect(lanes()).toEqual(["delta"]);
  expect(head("delta")).toContain("held · deploy freeze");
});

test("with every project idle and not held, Running draws no lane and says nothing is running or queued", async () => {
  await mountAll("#/", [statusOf("/srv/dev/alpha", []), statusOf("/srv/dev/beta", []), statusOf("/srv/dev/gamma", [])]);
  expect(lanes()).toEqual([]);
  expect(running().textContent).toContain("Nothing running and nothing queued.");
});

test("a backticked span in a running row's title shows as inline code, and its accessible name drops the backticks", async () => {
  const live = runOf(7, "DEMO-7", "Read a `+++` TOML header", { phase: "reviewing", round: 1, agent_ms: 16 * MINUTE, time_box_ms: 25 * MINUTE });
  await mount("#/", statusOf("/srv/dev/demo", [live]), statusOf("/srv/dev/writer", []));
  const row = within(lane("demo")).getByRole("link", { name: "DEMO-7 Read a +++ TOML header, reviewing, round 2 of 2, 16 of 25 minutes" });
  expect([...row.querySelectorAll("code")].map((code) => code.textContent)).toEqual(["+++"]);
});

const onCiRows = (name: string) => [...lane(name).querySelectorAll<HTMLElement>("[data-on-ci]")];
const laneItems = (name: string) =>
  within(lane(name)).getAllByRole("list").map((list) =>
    [...list.children].map((item) => (item.hasAttribute("data-on-ci-divider") ? item.getAttribute("aria-label") : item.hasAttribute("data-on-ci") ? "on-ci" : item.querySelector("a[data-row]") ? "run" : item.textContent)),
  );

test("a run fixing a pull request names it under its title, and the lane lists its pull requests on CI in its one list under the runs without counting them busy", async () => {
  const fixing = runOf(20, "DEMO-20", "Fix the slot", { phase: "working", round: 1, max_rounds: 3, pr_url: "https://github.com/example/demo/pull/77" });
  await mountAll("#/", [statusOf("/srv/dev/demo", [fixing], { workers: 2 }), statusOf("/srv/dev/writer", [])], {}, [draftPark, readyPark, ciPark]);
  const row = within(lane("demo")).getByRole("link", { name: /^DEMO-20 / }).closest("li")!;
  expect(row.querySelector("[data-fixing]")!.textContent).toBe("fixing #77");
  expect(laneItems("demo")).toEqual([["run", "On CI", "on-ci"]]);
  expect([...lane("demo").querySelectorAll("p")].map((p) => p.textContent)).not.toContain("On CI");
  expect(onCiRows("demo").map((ci) => [...ci.children].map((cell) => cell.textContent))).toEqual([["#41", "DEMO-8", "Ticket 8", "checks running · 4m"]]);
  expect(head("demo")).toContain("1 of 2 workers busy");
  expect(running().querySelector("header")!.textContent).toContain("1 of 3 workers busy");
});

test("a project whose only pull request is on CI keeps its Running lane, its head counting it on CI with nothing busy, and draws no Pull requests lane", async () => {
  await mountAll("#/", [statusOf("/srv/dev/demo", [], { workers: 2 }), statusOf("/srv/dev/writer", [])], {}, [ciPark]);
  expect(lanes()).toEqual(["demo"]);
  expect(head("demo")).toContain("0 of 2 workers busy");
  expect(head("demo")).toContain("· 1 on CI");
  expect(head("demo")).not.toContain("Nothing running.");
  expect(laneItems("demo")).toEqual([["On CI", "on-ci"]]);
  const prs = screen.getByRole("region", { name: "Pull requests" });
  expect(within(prs).queryAllByRole("group")).toEqual([]);
});

test("a reviewing run past its review cap names its round without the cap once, in its chip, and one within the cap names both", async () => {
  const url = "https://github.com/example/demo/pull/2404";
  const past = runOf(20, "DEMO-20", "Fix the slot", { phase: "reviewing", round: 6, max_rounds: 2, pr_url: url });
  const fresh = runOf(21, "DEMO-21", "Fresh run", { phase: "reviewing", round: 0, max_rounds: 2, pr_url: null });
  await mount("#/", statusOf("/srv/dev/demo", [past, fresh], { workers: 2 }), statusOf("/srv/dev/writer", []));
  const rowOf = (ticket: string) => within(lane("demo")).getByRole("link", { name: new RegExp(`^${ticket} `) }).closest("li")!;
  expect(within(rowOf("DEMO-20")).getByText("reviewing · round 7")).toBeTruthy();
  expect(within(lane("demo")).getByRole("link", { name: "DEMO-20 Fix the slot, reviewing, round 7, 0 of 25 minutes" })).toBeTruthy();
  expect(rowOf("DEMO-20").querySelector("[data-fixing]")!.textContent).toBe("fixing #2404");
  expect(within(rowOf("DEMO-20")).getByRole("link", { name: "#2404" }).getAttribute("href")).toBe(url);
  expect(rowOf("DEMO-20").textContent).not.toContain(" of 2");
  expect(within(rowOf("DEMO-21")).getByText("reviewing · round 1 of 2")).toBeTruthy();
  expect(rowOf("DEMO-21").querySelector("[data-fixing]")).toBeNull();
});

test("a run's row names a pending pause or abort without its note, and its fixing line names a check-fix round when one runs", async () => {
  const url = "https://github.com/example/demo/pull/2404";
  const paused = runOf(30, "DEMO-30", "Pause it", { phase: "merge_gate", stop_requested: "reboot writer", stop_action: "pause", step: "fixing checks (round 1 of 2)", pr_url: url });
  const aborted = runOf(31, "DEMO-31", "Abort it", { phase: "working", stop_requested: "wrong spec", stop_action: "abort", step: null, pr_url: url });
  const closed = runOf(32, "DEMO-32", "Close it", { phase: "working", stop_requested: "duplicate", stop_action: "abort_close", step: null, pr_url: null });
  await mount("#/", statusOf("/srv/dev/demo", [paused, aborted, closed], { workers: 3 }), statusOf("/srv/dev/writer", []));
  const rowOf = (ticket: string) => within(lane("demo")).getByRole("link", { name: new RegExp(`^${ticket} `) }).closest("li")!;
  expect(rowOf("DEMO-30").textContent).toContain("Pausing after this turn");
  expect(rowOf("DEMO-30").textContent).not.toContain("Pause requested");
  expect(rowOf("DEMO-30").textContent).not.toContain("reboot writer");
  expect(rowOf("DEMO-31").textContent).toContain("Aborting");
  expect(rowOf("DEMO-32").textContent).toContain("Aborting");
  expect(rowOf("DEMO-30").querySelector("[data-fixing]")!.textContent).toBe("fixing checks (round 1 of 2) on #2404");
  expect(rowOf("DEMO-31").querySelector("[data-fixing]")!.textContent).toBe("fixing #2404");
});

const witnessesOf = (keys: string[], elapsedMs: number) => ({ story: "DEMO-20", keys, started_ms: NOW - elapsedMs, elapsed_ms: elapsedMs });
const witnessLine = (name: string) => lane(name).querySelector("[data-witnesses]");

test("a running witness set keeps an idle project's lane, names its story, key range and time, and counts no worker busy", async () => {
  const witnesses = witnessesOf(["W3", "W4", "W5", "W6"], 4 * MINUTE + 30_000);
  await mount("#/", statusOf("/srv/dev/demo", [], { workers: 2, witnesses }), statusOf("/srv/dev/writer", []));
  expect(lanes()).toEqual(["demo"]);
  expect(head("demo")).toContain("0 of 2 workers busy");
  expect(head("demo")).not.toContain("Nothing running.");
  expect(witnessLine("demo")!.textContent).toBe("Witnesses · DEMO-20 · W3–W6 · running 4m");
  expect(witnessLine("demo")!.querySelector("a")).toBeNull();
});

test("a witness set beside a live run lists keys that do not run consecutively and leaves the busy count to the run", async () => {
  const witnesses = witnessesOf(["W1", "W3"], 12 * MINUTE);
  await mount("#/", statusOf("/srv/dev/demo", [demoRun], { workers: 2, witnesses }), statusOf("/srv/dev/writer", []));
  expect(within(lane("demo")).getAllByRole("link")).toHaveLength(1);
  expect(head("demo")).toContain("1 of 2 workers busy");
  expect(witnessLine("demo")!.textContent).toBe("Witnesses · DEMO-20 · W1, W3 · running 12m");
});

test("with no witness set running an idle project draws no lane and a live one no witness line", async () => {
  await mount("#/", statusOf("/srv/dev/demo", [], { witnesses: null }), statusOf("/srv/dev/writer", []));
  expect(lanes()).toEqual([]);
  cleanup();

  await mount("#/", statusOf("/srv/dev/demo", [demoRun], { witnesses: null }), statusOf("/srv/dev/writer", []));
  expect(lanes()).toEqual(["demo"]);
  expect(witnessLine("demo")).toBeNull();
});
