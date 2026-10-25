import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ScoutDetail, ScoutEvent, ScoutTask } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const [servedTask] = served.tasks;
const servedReport = served.reports[0]!;
const servedAction = served.actions[0]!;

const at = (hour: number, minute: number, second = 0) => new Date(2026, 9, 15, hour, minute, second).getTime();

const event = (id: number, kind: string, when: number): ScoutEvent => ({ id, at: when, kind, summary: kind.replaceAll("_", " ") });
const task = (question: string, pass: number, phase: ScoutTask["phase"], started: number | null, ended: number | null = null): ScoutTask => ({
  ...servedTask!, question, pass, wave: 1, phase, route: "luna", mode: "read", tokens: null,
  started_ms: started, ended_ms: ended, duration_ms: started != null && ended != null ? ended - started : null,
});
const report = (pass: number, when: number, findings: number, actions: number) => ({
  ...servedReport, pass, at: when,
  report: {
    ...servedReport.report, summary: `Pass ${pass} report.`,
    findings: Array.from({ length: findings }, () => servedReport.report.findings[0]!),
    actions: Array.from({ length: actions }, () => servedReport.report.actions[0]!),
  },
});

const FIRST = task("Where does the loop open SQLite connections?", 1, "done", at(14, 3), at(14, 10));
const DIGEST = task("Does the scheduled digest take the write lock during a publish?", 2, "done", at(14, 44, 30), at(14, 48));
const JOBS = task("Which other jobs call publish inside a store transaction?", 2, "done", at(14, 44, 40), at(14, 47));

const done: ScoutDetail = {
  ...served,
  scout: {
    ...served.scout, id: 31, ask: "Why do loop runs crash with database is locked?", kind: "bug", phase: "done", reason: null,
    lead: "opus", helper: "luna", swarm: 4, created_ms: at(14, 1), started_ms: at(14, 2), ended_ms: at(14, 35),
    budget: { ...served.scout.budget, minutes: 60, waves: 1 },
  },
  tasks: [FIRST, DIGEST, JOBS],
  reports: [report(1, at(14, 35), 4, 4), report(2, at(14, 51, 10), 1, 1)],
  actions: [1, 2, 3, 4, 5].map((position) => ({ ...servedAction, position, pass: position === 5 ? 2 : 1 })),
  events: [
    event(1, "scout_answer_started", at(14, 40, 10)),
    event(2, "scout_answered", at(14, 41, 2)),
    event(3, "scout_pass_started", at(14, 44, 5)),
    event(4, "scout_pass_done", at(14, 51, 15)),
  ],
  thread: [],
};

const PASS_THREE = at(15, 0);
const passThree = (phase: ScoutDetail["scout"]["phase"], tasks: ScoutTask[]): ScoutDetail => ({
  ...done,
  scout: { ...done.scout, phase },
  tasks: [...done.tasks, ...tasks],
  events: [...done.events, event(5, "scout_pass_started", PASS_THREE)],
});
const swarming = passThree("swarming", [
  task("Does the nightly export hold the lock too?", 3, "done", at(15, 0, 30), at(15, 2)),
  task("Does the retry loop reopen its connection?", 3, "running", at(15, 1)),
]);

/** DEMO answering `/scouts/31` with `bodies.scout`, read at each request. */
function scoutFetch(bodies: { scout: ScoutDetail }): Fetch {
  const polls = fleet({ items: [] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/scouts/31`) return Response.json(bodies.scout);
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: true });
  };
}

async function mount(scout: ScoutDetail, now: number) {
  const bodies = { scout };
  window.location.hash = "#/scouts/demo/31";
  const fake = fakeDeps(scoutFetch(bodies));
  fake.clock.now = now;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return { queries, bodies };
}

const settled = async () => {
  for (let round = 0; round < 4; round += 1) await act(settle);
};
const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  await settled();
};

const status = () => document.querySelector("header [role='status']")!.textContent;
const facts = () => document.querySelector("[data-scout-facts]")!.textContent!;
const stage = () => screen.getByRole("group", { name: "Stage" }).textContent;
const groups = () => Array.from(screen.getByRole("list", { name: "Scout activity" }).children) as HTMLElement[];
const row = (group: HTMLElement) => [group.querySelector("[data-group-title]")!.textContent, group.querySelector("[data-group-span]")!.textContent];
const titled = (title: string) => groups().filter((group) => group.querySelector("[data-group-title] b")!.textContent === title);
const fold = (group: HTMLElement) => group.querySelector(":scope > div > button[aria-expanded]")!;
const opened = (group: HTMLElement) => fold(group).getAttribute("aria-expanded") === "true";
const tone = (group: HTMLElement) => group.querySelector("[data-badge]")!.getAttribute("data-badge");
const helpers = (group: HTMLElement) =>
  within(within(group).getByRole("list", { name: "Helpers" })).getAllByRole("listitem").map((li) => li.querySelector("[data-question]")!.textContent);

const FIRST_RUN = [
  ["Plan · opus · 1 sub-task", "1m 00s"],
  ["Wave 1 · luna ×1 · 1 of 1 back", "7m 00s"],
  ["Report · opus · 4 findings · 4 action items", "25m 00s"],
];
const ANSWERED = ["Answer · opus", "52s"];
const PASS_TWO = ["Pass 2 · luna ×2 · 2 of 2 back · 1 finding · 1 action item", "7m 10s"];

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

test("a done scout's Progress ends on its Answer, then its Pass 2 open on its helpers, with Answer closed", async () => {
  await mount(done, at(15, 0));
  expect(groups().map(row)).toEqual([...FIRST_RUN, ANSWERED, PASS_TWO]);
  const [answer] = titled("Answer");
  const [pass] = titled("Pass 2");
  expect([opened(answer!), opened(pass!)]).toEqual([false, true]);
  expect([tone(answer!), tone(pass!)]).toEqual(["ok", "ok"]);
  expect(helpers(pass!)).toEqual([DIGEST.question, JOBS.question]);
});

test("a done scout's facts end on its newest pass and its minutes after the first run's helpers, and its stage names the pass", async () => {
  await mount(done, at(15, 0));
  expect(facts().endsWith(" · lead opus · helpers luna ×4 · 1 of 1 back · pass 2 7m")).toBe(true);
  expect(stage()).toBe("done · pass 2");
});

test("a scout swarming in pass 3 reads the pass in its status, stage, facts and a live last group, with Stop", async () => {
  await mount(swarming, PASS_THREE + 3 * 60_000);
  expect(status()).toBe("Pass 3: 1 of 2 helpers are back, and 1 is running.");
  expect(stage()).toBe("swarming · pass 3");
  expect(facts().endsWith(" · pass 3 3m")).toBe(true);
  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "Progress" }).querySelector("[data-live]")).toBeTruthy();
  const last = groups().at(-1)!;
  expect(row(last)).toEqual(["Pass 3 · luna ×2 · 1 of 2 back", "now"]);
  expect(opened(last)).toBe(true);
});

test("during pass 3 the first run's groups read as ended, with no running or pending row of their own", async () => {
  await mount(swarming, PASS_THREE + 3 * 60_000);
  const rows = groups().map(row);
  expect(rows.slice(0, -1)).toEqual([...FIRST_RUN, ANSWERED, PASS_TWO]);
  expect(rows.filter(([, span]) => span === "now")).toHaveLength(1);
  expect(rows.filter(([title, span]) => (title === "Synthesis · opus" || title === "Report · opus") && span === "")).toEqual([]);
});

test("a pass 3 planning and then synthesizing says so in its status and stage", async () => {
  await mount(passThree("planning", []), PASS_THREE + 60_000);
  expect([status(), stage()]).toEqual(["Pass 3: the lead is planning.", "planning · pass 3"]);
  cleanup();
  await mount(passThree("synthesizing", [task("Does the nightly export hold the lock too?", 3, "done", at(15, 0, 30), at(15, 2))]), PASS_THREE + 4 * 60_000);
  expect([status(), stage()]).toEqual(["Pass 3: the lead is writing its report.", "synthesizing · pass 3"]);
});

test("a pass synthesizing after its helpers reads now, with the tone of a stopped, failed or finished helper", async () => {
  const cases: [ScoutTask["phase"], string][] = [["stopped", "wait"], ["failed", "bad"], ["done", "ok"]];
  for (const [phase, says] of cases) {
    await mount(passThree("synthesizing", [task("Does the nightly export hold the lock too?", 3, phase, at(15, 0, 30), at(15, 2))]), PASS_THREE + 4 * 60_000);
    const last = groups().at(-1)!;
    expect([row(last)[1], tone(last)]).toEqual(["now", says]);
    cleanup();
  }
});

test("a failed try at pass 2 and its retry read as two Pass 2 groups, each holding only its own span's helpers", async () => {
  const failed = task("Does the digest hold the lock?", 2, "done", at(14, 44, 30), at(14, 45));
  const retried = task("Does the digest hold the lock across a publish?", 2, "done", at(14, 50, 30), at(14, 53));
  const retry: ScoutDetail = {
    ...done,
    tasks: [FIRST, failed, retried],
    reports: [report(1, at(14, 35), 4, 4), report(2, at(14, 54, 50), 1, 1)],
    events: [
      event(3, "scout_pass_started", at(14, 44, 5)),
      event(4, "scout_pass_failed", at(14, 46)),
      event(5, "scout_pass_started", at(14, 50)),
      event(6, "scout_pass_done", at(14, 55)),
    ],
  };
  await mount(retry, at(15, 0));
  const [first, second] = titled("Pass 2");
  expect([tone(first!), tone(second!)]).toEqual(["bad", "ok"]);
  await click(fold(first!));
  expect(helpers(first!)).toEqual([failed.question]);
  expect(helpers(second!)).toEqual([retried.question]);
  const thread = within(screen.getByRole("complementary", { name: "Thread" })).getByRole("list", { name: "Thread" });
  const starts = Array.from(thread.querySelectorAll("[data-thread-line]"), (line) => line.textContent!).filter((text) => !text.includes("done") && text.startsWith("pass"));
  expect(starts).toEqual(["pass 2 · luna ×1 · 14:44", "pass 2 · luna ×1 · 14:50"]);
});

test("a live retry of pass 3 holds its own unstarted helpers, not those its stopped try left queued", async () => {
  const numbered = (id: number, each: ScoutTask): ScoutTask => ({ ...each, id });
  const ran = numbered(10, task("Does the nightly export hold the lock?", 3, "stopped", at(15, 0, 30), at(15, 1)));
  const left = numbered(11, task("Does the backup job hold the lock?", 3, "queued", null));
  const running = numbered(12, task("Does the nightly export hold the lock across a vacuum?", 3, "running", at(15, 5, 30)));
  const waiting = numbered(13, task("Does the vacuum take the lock?", 3, "queued", null));
  const retry: ScoutDetail = {
    ...done,
    scout: { ...done.scout, phase: "swarming" },
    tasks: [...done.tasks, ran, left, running, waiting],
    events: [...done.events, event(5, "scout_pass_started", PASS_THREE), event(6, "scout_pass_stopped", at(15, 1)), event(7, "scout_pass_started", at(15, 5))],
  };
  await mount(retry, at(15, 7));
  const live = groups().at(-1)!;
  expect(row(live)[0]).toBe("Pass 3 · luna ×2 · 0 of 2 back");
  expect(helpers(live)).toEqual([running.question, waiting.question]);
  expect(status()).toBe("Pass 3: 1 of 2 helpers are running, and 1 waits for a slot.");
});

test("a live retry of pass 3 holds every queued helper before its first starts, and its failed try none of them", async () => {
  const numbered = (id: number, each: ScoutTask): ScoutTask => ({ ...each, id });
  const ran = numbered(10, task("Does the nightly export hold the lock?", 3, "failed", at(15, 0, 30), at(15, 1)));
  const left = numbered(11, task("Does the backup job hold the lock?", 3, "queued", null));
  const first = numbered(12, task("Does the nightly export hold the lock across a vacuum?", 3, "queued", null));
  const second = numbered(13, task("Does the vacuum take the lock?", 3, "queued", null));
  const retry = (tasks: ScoutTask[]): ScoutDetail => ({
    ...done,
    scout: { ...done.scout, phase: "swarming" },
    tasks: [...done.tasks, ran, left, ...tasks],
    events: [...done.events, event(5, "scout_pass_started", PASS_THREE), event(6, "scout_pass_failed", at(15, 1)), event(7, "scout_pass_started", at(15, 5))],
  });
  const { queries, bodies } = await mount(retry([first, second]), at(15, 7));
  const [failedTry, live] = titled("Pass 3");
  expect(row(live!)[0]).toBe("Pass 3 · luna ×3 · 0 of 3 back");
  expect(helpers(live!)).toEqual([left.question, first.question, second.question]);
  expect(helpers(failedTry!)).toEqual([ran.question]);
  expect(status()).toBe("Pass 3: 3 of 3 helpers wait for a slot.");
  bodies.scout = retry([{ ...first, phase: "running", started_ms: at(15, 6) }, second]);
  await act(async () => {
    await queries.refetchQueries();
  });
  await settled();
  expect(row(groups().at(-1)!)[0]).toBe("Pass 3 · luna ×2 · 0 of 2 back");
  expect(helpers(groups().at(-1)!)).toEqual([first.question, second.question]);
});

test("an answer turn still running reads now, and one stopped takes the wait tone", async () => {
  const asking = (kinds: string[], phase: ScoutDetail["scout"]["phase"]): ScoutDetail => ({
    ...done, scout: { ...done.scout, phase }, tasks: [FIRST], reports: [report(1, at(14, 35), 4, 4)],
    events: kinds.map((kind, index) => event(index + 1, kind, at(14, 40, 10 + index * 30))),
  });
  await mount(asking(["scout_answer_started"], "answering"), at(14, 42));
  expect(row(groups().at(-1)!)).toEqual(["Answer · opus", "now"]);
  cleanup();
  await mount(asking(["scout_answer_started", "scout_answer_stopped"], "done"), at(14, 42));
  expect(tone(titled("Answer")[0]!)).toBe("wait");
});

test("a Pass group the reader closed stays closed when the next poll brings a helper back", async () => {
  const { queries, bodies } = await mount(swarming, PASS_THREE + 3 * 60_000);
  await click(fold(groups().at(-1)!));
  expect(opened(groups().at(-1)!)).toBe(false);
  bodies.scout = { ...swarming, tasks: swarming.tasks.map((each) => (each.phase === "running" ? { ...each, phase: "done", ended_ms: at(15, 3), duration_ms: 120_000 } : each)) };
  await act(async () => {
    await queries.refetchQueries();
  });
  await settled();
  expect(row(groups().at(-1)!)[0]).toBe("Pass 3 · luna ×2 · 2 of 2 back");
  expect(opened(groups().at(-1)!)).toBe(false);
});

test("Stop during a pass says the pass stops and the report stays; during a first run it says no report is written", async () => {
  const line = async () => {
    await click(screen.getByRole("button", { name: "Stop" }));
    return within(screen.getByRole("dialog", { name: "Stop scout 31?" })).getByText(/stops? now/).textContent;
  };
  await mount(swarming, PASS_THREE + 3 * 60_000);
  expect(await line()).toBe("Pass 3 stops now. The report so far stays.");
  cleanup();
  const firstRun: ScoutDetail = { ...done, scout: { ...done.scout, phase: "swarming", ended_ms: null }, tasks: [{ ...FIRST, phase: "running", ended_ms: null }], reports: [], actions: [], events: [] };
  await mount(firstRun, at(14, 5));
  expect(await line()).toBe("Its lead and helpers stop now, and it writes no report.");
});
