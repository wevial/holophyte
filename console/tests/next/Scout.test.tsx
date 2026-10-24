import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ScoutDetail, ScoutRow, Scouts, ScoutTask } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { ACTIONS_OFF_LINE } from "../../src/next/components/CardActions";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const [servedTask] = served.tasks;
const servedReport = served.reports[0]!;

const at = (hour: number, minute: number, second = 0) => new Date(2026, 9, 15, hour, minute, second).getTime();
const NOON = at(12, 0);
const ASK = "Why do loop runs crash with database is locked?";

const task = (question: string, phase: ScoutTask["phase"], fields: Partial<ScoutTask> = {}): ScoutTask => ({
  ...servedTask!, question, phase, route: "luna", mode: "read", tokens: null,
  started_ms: phase === "queued" ? null : at(11, 47, 12), ended_ms: null, duration_ms: null, ...fields,
});
const back = (question: string, fields: Partial<ScoutTask> = {}) =>
  task(question, "done", { ended_ms: at(11, 49), duration_ms: 108_000, ...fields });

const waveOne: ScoutTask[] = [
  back("Where does the loop open SQLite connections?"),
  back("Which writers hold a transaction open?"),
  back("Does the listener write from its own thread?"),
  back("Is WAL mode set on every connection?"),
  back("What do the locked lines have in common?"),
  task("Repro: two loop workers claim at once", "running", { mode: "run" }),
  task("Repro: listener writes during a publish", "running", { mode: "run" }),
  task("Does busy_timeout survive the reconnect?", "running"),
  task("Which tests cover concurrent writers?", "running"),
  task("Does the digest open its own connection?", "queued"),
];

const live: ScoutDetail = {
  ...served,
  scout: {
    ...served.scout, id: 31, ask: ASK, kind: "bug", phase: "swarming", reason: null, lead: "opus", helper: "luna", swarm: 4,
    base_sha: "a41c9e2b7d0f4c3e9a1b2c3d4e5f60718293a4b5", created_ms: at(11, 45), started_ms: at(11, 46), ended_ms: null,
    budget: { ...served.scout.budget, minutes: 30, waves: 2 },
  },
  tasks: waveOne,
  reports: [],
  actions: [],
  events: [{ at: at(11, 47), kind: "agent_turn", summary: "scout-lead turn ended" }],
};

/** `live` with the first done task's tokens, so its row reads them. */
const tokened: ScoutDetail = { ...live, tasks: [back(waveOne[0]!.question, { tokens: 38_000 }), ...waveOne.slice(1)] };

const report = (summary: string, findings = 0, actions = 0) => ({
  ...servedReport,
  report: {
    ...servedReport.report, summary,
    findings: Array.from({ length: findings }, () => servedReport.report.findings[0]!),
    actions: Array.from({ length: actions }, () => servedReport.report.actions[0]!),
  },
});

const twoWaves: ScoutDetail = {
  ...live,
  scout: { ...live.scout, phase: "done", started_ms: at(11, 0), ended_ms: at(11, 33, 42) },
  tasks: [
    ...waveOne.map((each, index) => ({
      ...each, phase: "done" as const, started_ms: at(11, 1, 12), ended_ms: index === 9 ? at(11, 17, 32) : at(11, 10), duration_ms: 60_000,
    })),
    task("Does publish need the transaction open?", "done", { wave: 2, started_ms: at(11, 20, 12), ended_ms: at(11, 25), duration_ms: 130_000, tokens: 34_000 }),
    task("Does the stall line up with disk latency?", "stopped", { wave: 2, started_ms: at(11, 22), ended_ms: at(11, 30, 12), duration_ms: 480_000 }),
  ],
  reports: [report("Reproduced in a failing test.", 4, 4)],
  events: [],
};

interface Daemon {
  scouts?: Record<number, ScoutDetail>;
  actions?: boolean;
  stop?: () => Response;
  list?: ScoutRow[];
}

/** DEMO answering `/scouts/N` from `daemon.scouts`, read at each request, 404 for any other; every request is kept. */
function scoutFetch(daemon: Daemon) {
  const seen: { url: string; method: string; body: unknown }[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fetch: Fetch = async (url, init) => {
    seen.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    const id = new RegExp(`^${DEMO}/scouts/(\\d+)$`).exec(url)?.[1];
    if (id != null) {
      const detail = daemon.scouts?.[Number(id)];
      return detail ? Response.json(detail) : Response.json({ error: "no such scout" }, { status: 404 });
    }
    if (url === `${DEMO}/scouts`) return Response.json({ ...servedScouts, scouts: daemon.list ?? [] });
    if (url === `${DEMO}/actions/scout-stop` && daemon.stop) return daemon.stop();
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: daemon.actions ?? true });
  };
  return { fetch, seen };
}

async function mount(hash: string, fetch: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOON;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return queries;
}

const settled = async () => {
  for (let round = 0; round < 4; round += 1) await act(settle);
};
const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  await settled();
};

const status = () => document.querySelector("header [role='status']")!.textContent;
const facts = () => document.querySelector("[data-scout-facts]")!.textContent;
const activity = () => screen.getByRole("list", { name: "Scout activity" });
const groups = () => Array.from(activity().children) as HTMLElement[];
const row = (group: HTMLElement) => ({
  time: group.querySelector(":scope > div > time")?.textContent ?? null,
  title: group.querySelector("[data-group-title]")!.textContent,
  span: group.querySelector("[data-group-span]")!.textContent,
});
const fold = (group: HTMLElement) => group.querySelector(":scope > div > button[aria-expanded]");
const opened = (group: HTMLElement) => fold(group)?.getAttribute("aria-expanded") === "true";
const groupTitled = (title: string) => groups().find((group) => group.querySelector("[data-group-title] b")!.textContent!.startsWith(title))!;
const helpers = (group: HTMLElement) => within(within(group).getByRole("list", { name: "Helpers" })).getAllByRole("listitem");
const helperRow = (li: HTMLElement) => ({
  state: within(li).getByRole("img").getAttribute("aria-label"),
  question: li.querySelector("[data-question]")!.textContent,
  mode: li.querySelector("[data-mode]")!.textContent,
  route: li.querySelector("[data-route]")!.textContent,
  took: li.querySelector("[data-took]")!.textContent,
  tokens: li.querySelector("[data-tokens]")!.textContent,
});
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";

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

test("a swarming scout's header reads its ask, breadcrumb, facts, wave sentence and stage", async () => {
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: live } }).fetch);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(ASK);
  expect(screen.getByRole("navigation", { name: "Breadcrumb" }).textContent).toBe("Scouts / demo / scout 31");
  expect(facts()).toBe("scout 31 · bug · demo · base a41c9e2 · started 11:46 PDT · 14m of a 30m budget · lead opus · helpers luna ×4 · 5 of 10 back");
  expect(status()).toBe("Wave 1: 5 of 10 helpers are back, 4 are running, and 1 waits for a slot.");
  expect(screen.getByRole("group", { name: "Stage" }).textContent).toBe("swarming · wave 1 of 2");
});

test("the breadcrumb links Scouts to the list and the project to the list scoped to it", async () => {
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: live } }).fetch);
  const crumbs = within(screen.getByRole("navigation", { name: "Breadcrumb" })).getAllByRole("link");
  expect(crumbs.map((link) => link.getAttribute("href"))).toEqual(["/next.html#/scouts", "/next.html#/scouts?project=demo"]);
});

test("a done, failed, stopped or queued scout's status says its report, failure, stop or wait", async () => {
  const cases: [Partial<ScoutDetail["scout"]>, ScoutDetail["reports"], string][] = [
    [{ phase: "done", ended_ms: at(11, 58) }, [report("Reproduced in a failing test.")], "Reproduced in a failing test."],
    [{ phase: "failed", ended_ms: at(11, 58), reason: "lead route opus is down: probe timed out\nmore" }, [], "Failed: lead route opus is down: probe timed out"],
    [{ phase: "stopped", ended_ms: at(11, 58), reason: "stopped by maintainer: wrong project" }, [], "Stopped by maintainer: wrong project."],
    [{ phase: "queued", started_ms: null }, [], "Waiting for a scout slot."],
  ];
  for (const [scout, reports, says] of cases) {
    await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: { ...live, scout: { ...live.scout, ...scout }, reports } } }).fetch);
    expect(status()).toBe(says);
    cleanup();
  }
});

test("a queued scout's facts end on when it was queued", async () => {
  const queued = { ...live, scout: { ...live.scout, id: 29, kind: "research" as const, phase: "queued" as const, started_ms: null, created_ms: at(11, 58) }, tasks: [] };
  await mount("#/scouts/demo/29", scoutFetch({ scouts: { 29: queued } }).fetch);
  expect(facts()).toBe("scout 29 · research · demo · queued 11:58 PDT");
});

test("a scout the project lacks and a project the console lacks each read their notice", async () => {
  await mount("#/scouts/demo/99", scoutFetch({ scouts: { 31: live } }).fetch);
  expect(screen.getByText("demo has no scout 99.")).toBeTruthy();
  cleanup();
  await mount("#/scouts/nope/31", scoutFetch({ scouts: { 31: live } }).fetch);
  expect(screen.getByText("No project named nope answers this console.")).toBeTruthy();
});

test("a live scout's activity reads Plan, the running wave and the pending Synthesis, with only the wave open", async () => {
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: live } }).fetch);
  expect(groups().map(row)).toEqual([
    { time: "11:46", title: "Plan · opus · 10 sub-tasks", span: "1m 12s" },
    { time: "11:47", title: "Wave 1 · luna ×4 · 5 of 10 back", span: "now" },
    { time: null, title: "Synthesis · opus", span: "" },
  ]);
  expect([opened(groupTitled("Plan")), opened(groupTitled("Wave"))]).toEqual([false, true]);
});

test("the open wave lists its helpers in plan order with state, mode, route, time and tokens", async () => {
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: tokened } }).fetch);
  const rows = helpers(groupTitled("Wave")).map(helperRow);
  expect(rows.map((each) => each.question)).toEqual(waveOne.map((each) => each.question));
  expect(rows[0]).toEqual({ state: "back", question: waveOne[0]!.question, mode: "read", route: "luna", took: "1m 48s", tokens: "38k tok" });
  expect(rows[5]).toMatchObject({ state: "running", mode: "run", tokens: "…" });
  expect(rows[9]).toMatchObject({ state: "waiting", took: "–", tokens: "" });
});

test("a done scout of two waves reads Plan, Wave 1, Synthesis, Wave 2 and Report, opening only the wave with a stopped helper", async () => {
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: twoWaves } }).fetch);
  expect(groups().map((group) => [row(group).title, row(group).span])).toEqual([
    ["Plan · opus · 10 sub-tasks", "1m 12s"],
    ["Wave 1 · luna ×4 · 10 of 10 back", "16m 20s"],
    ["Synthesis · opus · 2 more sub-tasks", "2m 40s"],
    ["Wave 2 · luna ×2 · 1 of 2 back · 1 stopped", "10m 00s"],
    ["Report · opus · 4 findings · 4 action items", "3m 30s"],
  ]);
  expect(groups().filter(opened).map((group) => row(group).title)).toEqual(["Wave 2 · luna ×2 · 1 of 2 back · 1 stopped"]);
  const waveTwo = helpers(groupTitled("Wave 2")).map(helperRow);
  expect(waveTwo.map((each) => [each.state, each.tokens])).toEqual([["back", "34k tok"], ["stopped", "stopped"]]);
  expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
});

test("a scout with no tasks ends Plan on the lead's first turn, and Report's steps hold the events after it", async () => {
  const bare: ScoutDetail = {
    ...live,
    scout: { ...live.scout, phase: "done", started_ms: at(11, 1), ended_ms: at(11, 3) },
    tasks: [],
    reports: [report("Nothing to scout.")],
    events: [
      { at: at(11, 2), kind: "agent_turn", summary: "scout-lead turn ended" },
      { at: at(11, 2, 5), kind: "scout_helpers", summary: "helper route luna is down: probe failed" },
    ],
  };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: bare } }).fetch);
  expect(groups().map((group) => [row(group).title.split(" · ")[0], row(group).span])).toEqual([["Plan", "1m 00s"], ["Report", "1m 00s"]]);
  await click(fold(groupTitled("Report"))!);
  const steps = Array.from(groupTitled("Report").querySelectorAll("[data-step-text]"), (step) => step.textContent);
  expect(steps).toContain("helper route luna is down: probe failed");
});

test("a scout stopped mid-wave ends that wave at the stop, cut short rather than live", async () => {
  const stopped = { ...live, scout: { ...live.scout, phase: "stopped" as const, reason: "wrong project", ended_ms: at(11, 57, 12) } };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: stopped } }).fetch);
  const wave = groupTitled("Wave");
  expect(row(wave).span).toBe("10m 00s");
  expect(wave.querySelector("[data-badge]")!.getAttribute("data-badge")).toBe("wait");
  await click(fold(wave)!);
  expect(helpers(wave).map(helperRow)[5]!.took).toBe("10m 00s");
});

test("a synthesizing scout ends its wave on the last helper back, past an unlaunched one, and runs Synthesis", async () => {
  const synthesizing: ScoutDetail = {
    ...live,
    scout: { ...live.scout, phase: "synthesizing" },
    tasks: [back(waveOne[0]!.question, { started_ms: at(11, 47, 12), ended_ms: at(11, 50, 12) }), waveOne[9]!],
  };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: synthesizing } }).fetch);
  expect(groups().map((group) => [row(group).title, row(group).span])).toEqual([
    ["Plan · opus · 2 sub-tasks", "1m 12s"],
    ["Wave 1 · luna ×2 · 1 of 2 back", "3m 00s"],
    ["Synthesis · opus", "now"],
  ]);
});

test("a swarming second wave ends the first on its last helper back, past one never started, so Synthesis spans the gap", async () => {
  const second: ScoutDetail = {
    ...live,
    tasks: [
      back(waveOne[0]!.question, { started_ms: at(11, 47, 12), ended_ms: at(11, 50, 12) }),
      waveOne[9]!,
      task("Does publish need the transaction open?", "running", { wave: 2, started_ms: at(11, 52) }),
    ],
  };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: second } }).fetch);
  expect(groups().map((group) => [row(group).title, row(group).span])).toEqual([
    ["Plan · opus · 2 sub-tasks", "1m 12s"],
    ["Wave 1 · luna ×2 · 1 of 2 back", "3m 00s"],
    ["Synthesis · opus · 1 more sub-task", "1m 48s"],
    ["Wave 2 · luna ×1 · 0 of 1 back", "now"],
    ["Report · opus", ""],
  ]);
});

test("a wave whose helpers never started leaves the lead's turn after it to Synthesis, then to Report", async () => {
  const refused = { ...waveOne[9]!, question: "Does the digest open its own connection?" };
  const turn = { at: at(11, 47), kind: "agent_turn", summary: "scout-lead turn ended" };
  const synthesizing: ScoutDetail = { ...live, scout: { ...live.scout, phase: "synthesizing" }, tasks: [refused], events: [turn] };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: synthesizing } }).fetch);
  expect(groups().map((group) => [row(group).title, row(group).span])).toEqual([
    ["Plan · opus · 1 sub-task", "1m 00s"],
    ["Wave 1 · luna ×1 · 0 of 1 back", ""],
    ["Synthesis · opus", "now"],
  ]);
  cleanup();
  const done: ScoutDetail = { ...synthesizing, scout: { ...live.scout, phase: "done", ended_ms: at(11, 50) }, reports: [report("Nothing came back.")] };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: done } }).fetch);
  expect(groups().map((group) => [row(group).title.split(" · ")[0], row(group).span])).toEqual([["Plan", "1m 00s"], ["Wave 1", ""], ["Report", "3m 00s"]]);
});

test("a done scout's wave ends on its last helper back, past one never started, before Report", async () => {
  const done: ScoutDetail = {
    ...live,
    scout: { ...live.scout, phase: "done", ended_ms: at(11, 55) },
    tasks: [back(waveOne[0]!.question, { ended_ms: at(11, 50, 12) }), waveOne[9]!],
    reports: [report("Capped at one helper.")],
    events: [{ at: at(11, 52), kind: "scout_report", summary: "the lead wrote its report" }],
  };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: done } }).fetch);
  expect(groups().map((group) => [row(group).title.split(" · ")[0], row(group).span])).toEqual([["Plan", "1m 12s"], ["Wave 1", "3m 00s"], ["Report", "4m 48s"]]);
  await click(fold(groupTitled("Report"))!);
  expect(Array.from(groupTitled("Report").querySelectorAll("[data-step-text]"), (step) => step.textContent)).toEqual(["the lead wrote its report"]);
});

test("a running Plan shows its steps open, with no control to fold them", async () => {
  const planning: ScoutDetail = {
    ...live,
    scout: { ...live.scout, phase: "planning" },
    tasks: [],
    events: [{ at: at(11, 50), kind: "scout_planning", summary: "the lead reads the repository" }],
  };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: planning } }).fetch);
  const plan = groupTitled("Plan");
  expect(row(plan).span).toBe("now");
  expect(fold(plan)).toBeNull();
  expect(Array.from(plan.querySelectorAll("[data-step-text]"), (step) => step.textContent)).toEqual(["the lead reads the repository"]);
});

test("a wave the reader closed stays closed when the next poll brings a helper back", async () => {
  const bodies = { 31: live };
  const queries = await mount("#/scouts/demo/31", scoutFetch({ scouts: bodies }).fetch);
  await click(fold(groupTitled("Wave"))!);
  expect(opened(groupTitled("Wave"))).toBe(false);
  bodies[31] = { ...live, tasks: live.tasks.map((each, index) => (index === 5 ? { ...each, phase: "done", ended_ms: at(11, 59), duration_ms: 60_000 } : each)) };
  await act(async () => {
    await queries.refetchQueries();
  });
  await settled();
  expect(row(groupTitled("Wave")).title).toBe("Wave 1 · luna ×4 · 6 of 10 back");
  expect(opened(groupTitled("Wave"))).toBe(false);
});

test("Stop asks for a reason, posts it once, closes on ok with focus on Stop and says so in the toast", async () => {
  const { fetch, seen } = scoutFetch({ scouts: { 31: live }, stop: () => Response.json({ ok: true, detail: "stop requested for scout 31" }) });
  await mount("#/scouts/demo/31", fetch);
  await click(screen.getByRole("button", { name: "Stop" }));
  const dialog = screen.getByRole("dialog", { name: "Stop scout 31?" });
  const confirm = within(dialog).getByRole("button", { name: "Stop scout" });
  expect(confirm.hasAttribute("disabled")).toBe(true);
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "wrong project" } }));
  await click(confirm);
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map((post) => [post.url, post.body])).toEqual([[`${DEMO}/actions/scout-stop`, { scout: 31, note: "wrong project" }]]);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(toast()).toBe("Stop sent to scout 31.");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Stop" }));
});

test("a sent stop that ends the scout puts focus on the ask once Stop is gone", async () => {
  const bodies: Record<number, ScoutDetail> = { 31: live };
  const stop = () => {
    bodies[31] = { ...live, scout: { ...live.scout, phase: "stopped", reason: "wrong project", ended_ms: at(11, 59) } };
    return Response.json({ ok: true, detail: "stop requested for scout 31" });
  };
  await mount("#/scouts/demo/31", scoutFetch({ scouts: bodies, stop }).fetch);
  await click(screen.getByRole("button", { name: "Stop" }));
  const dialog = screen.getByRole("dialog", { name: "Stop scout 31?" });
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "wrong project" } }));
  await click(within(dialog).getByRole("button", { name: "Stop scout" }));
  expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("heading", { level: 1, name: ASK }));
});

test("a refused stop reads inside the dialog, and Escape closes it with focus back on Stop", async () => {
  const stop = () => Response.json({ ok: false, detail: "scout 31 runs on another-host" });
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: live }, stop }).fetch);
  await click(screen.getByRole("button", { name: "Stop" }));
  const dialog = screen.getByRole("dialog", { name: "Stop scout 31?" });
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "wrong project" } }));
  await click(within(dialog).getByRole("button", { name: "Stop scout" }));
  expect(within(screen.getByRole("dialog", { name: "Stop scout 31?" })).getByRole("alert").textContent).toBe("scout 31 runs on another-host");
  await act(async () => void fireEvent.keyDown(document, { key: "Escape" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Stop" }));
});

test("with actions off Stop is disabled under the actions-off line", async () => {
  await mount("#/scouts/demo/31", scoutFetch({ scouts: { 31: live }, actions: false }).fetch);
  const stop = screen.getByRole("button", { name: "Stop" });
  expect(stop.hasAttribute("disabled")).toBe(true);
  expect(stop.getAttribute("title")).toBe(ACTIONS_OFF_LINE);
});

test("a row of the Scouts list links to its scout's page", async () => {
  const listed = { ...servedScouts.scouts[0]!, id: 31, ask: ASK };
  await mount("#/scouts", scoutFetch({ list: [listed] }).fetch);
  const link = within(screen.getByRole("list", { name: "Scouts, newest first" })).getByRole("link");
  expect(link.getAttribute("href")).toBe("/next.html#/scouts/demo/31");
  expect(link.textContent).toContain(ASK);
});
