import { afterAll, afterEach, beforeAll, expect, spyOn, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ScoutDetail, ScoutEvent, ScoutFinding, ScoutRow, Scouts, ScoutTask, ThreadMessage } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const [servedTask] = served.tasks;
const servedPass = served.reports[0]!;
const servedAction = served.actions[0]!;
const [servedMessage] = served.thread;
const [servedFile] = servedPass.report.findings[0]!.evidence;

const TITLE = "Measure test_babysit_threads.py against the 90 s ceiling";
const ASK = "tests/test_babysit_threads.py takes about 74 s in CI and 113 s on a slow runner. Measure it and propose the smallest change.";
const HEADLINE = "Not measured: every timing helper failed to start";
const BULLETS = [
  "Real sleeps aren't the cause: every wait is patched except one 1 s kill cap",
  "Likely cost: per-test setup, and tests that run two or three full loops",
  "One test runs setUp three times and throws the first away",
  "Next: measure on a working runner, then split the module",
];
const SUMMARY = "Every timing helper failed to start, so nothing was measured.";
const REASON = "could not start: implementer credential not set";
const WHAT = "scout 31 · bug · demo · base d149ad5 · started Sat 02:23 PDT";
const COST = "3m of 20m · lead opus · helpers luna ×3 · 12 of 12 back · 280k tok";

const STARTED = new Date(2026, 9, 24, 2, 23).getTime();
const WAVE = STARTED + 60_000;
const ENDED = STARTED + 231_000;

const task = (index: number, phase: ScoutTask["phase"], fields: Partial<ScoutTask>): ScoutTask => ({
  ...servedTask!, id: index, pass: 1, wave: 1, title: `Helper ${index}`, question: `Question ${index} about \`tests/loop_fixture.py\``, phase, route: "luna",
  started_ms: WAVE, ...fields,
});
const failed = (index: number) => task(index, "failed", { mode: "run", tokens: null, duration_ms: 0, ended_ms: WAVE, reason: REASON });
const back = (index: number, tokens: number, seconds: number) =>
  task(index, "done", { mode: "read", tokens, duration_ms: seconds * 1000, ended_ms: WAVE + seconds * 1000, reason: null });

const twelve = [
  failed(1), failed(2), failed(3), failed(4),
  back(5, 37_000, 31), back(6, 80_000, 46),
  failed(7),
  back(8, 32_000, 23), back(9, 80_000, 45),
  failed(10), failed(11),
  back(12, 51_000, 28),
];

const places = ["tests/fake_agent.py:232", "tests/fake_agent.py:244", "tests/loop_fixture.py:340", "tests/babysit_fixture.py:53", "tests/test_babysit_threads.py:88"];
const fileAt = (place: string) => {
  const [file, line] = place.split(":");
  return { ...servedFile!, file: file!, line: Number(line), verified: true, why: null };
};
const finding = (title: string, evidence: ScoutFinding["evidence"]): ScoutFinding => ({ title, status: "confirmed", evidence });
const hypothesis = (verdict: "ruled_out" | "open", index: number) => ({ hypothesis: `Hypothesis ${index}`, separates: `What separates ${index}`, verdict });

const message = (id: number, fields: Partial<ThreadMessage>): ThreadMessage => ({
  ...servedMessage!, id, blocks: [], answers: null, consumed_by: null, consumed_ms: null, ...fields,
});

const scout31: ScoutDetail = {
  ...served,
  scout: {
    ...served.scout, id: 31, title: TITLE, ask: ASK, kind: "bug", phase: "done", reason: null, lead: "opus", helper: "luna", swarm: 3,
    subject: null, base_sha: "d149ad5e0f1a2b3c4d5e6f708192a3b4c5d6e7f8", created_ms: STARTED - 60_000, started_ms: STARTED, ended_ms: ENDED,
    budget: { ...served.scout.budget, minutes: 20, waves: 1 },
  },
  tasks: twelve,
  reports: [{
    ...servedPass, pass: 1, at: ENDED,
    report: {
      ...servedPass.report, headline: HEADLINE, bullets: BULLETS, summary: SUMMARY,
      findings: [
        finding("The only real-time wait is the 1 s kill cap", places.map(fileAt)),
        finding("Which tests take the time is unmeasured", [fileAt("tests/test_babysit_threads.py:12")]),
      ],
      reproduction: { level: "not_reproduced", hypotheses: [hypothesis("ruled_out", 1), hypothesis("open", 2), hypothesis("ruled_out", 3), hypothesis("open", 4), hypothesis("open", 5)] },
    },
  }],
  actions: [1, 2].map((position) => ({ ...servedAction, position, pass: 1, title: `Item ${position}`, ticket: null, filed_ms: null })),
  events: [],
  thread: [
    message(1, { author: "maintainer", mode: "ask", text: "Which test is slowest?", at: ENDED + 60_000, consumed_by: 2, consumed_ms: ENDED + 61_000 }),
    message(2, { author: "scout", mode: "answer", answers: 1, text: "Nothing was timed, so no test is known to be slowest.", at: ENDED + 90_000 }),
  ],
};

/** `scout31` with its newest report's headline and bullets taken away. */
const summaryOnly: ScoutDetail = {
  ...scout31,
  reports: [{ ...scout31.reports[0]!, report: { ...scout31.reports[0]!.report, headline: null, bullets: null } }],
};

/** `scout31` as a research scout whose report has no reproduction. */
const research: ScoutDetail = {
  ...scout31,
  scout: { ...scout31.scout, kind: "research" },
  reports: [{ ...scout31.reports[0]!, report: { ...scout31.reports[0]!.report, reproduction: null } }],
};

const at = (hour: number, minute: number, second = 0) => new Date(2026, 9, 15, hour, minute, second).getTime();
const event = (id: number, kind: string, when: number): ScoutEvent => ({ id, at: when, kind, summary: kind.replaceAll("_", " ") });

const waveTask = (question: string, wave: number, started: number, ended: number, phase: ScoutTask["phase"] = "done"): ScoutTask => ({
  ...servedTask!, question, pass: 1, wave, phase, route: "luna", mode: "read", tokens: null, started_ms: started, ended_ms: ended, duration_ms: ended - started,
});

/** Two waves with Synthesis between, as the Scout page tests build them, and lead turns inside Plan and Report. */
const twoWaves: ScoutDetail = {
  ...scout31,
  scout: { ...scout31.scout, phase: "done", started_ms: at(11, 0), ended_ms: at(11, 33, 42), budget: { ...scout31.scout.budget, minutes: 30, waves: 2 } },
  tasks: [
    ...Array.from({ length: 10 }, (_, index) => waveTask(`Wave one question ${index}`, 1, at(11, 1, 12), at(11, 10))),
    waveTask("Does publish need the transaction open?", 2, at(11, 20, 12), at(11, 25)),
    waveTask("Does the stall line up with disk latency?", 2, at(11, 22), at(11, 30, 12), "stopped"),
  ],
  events: [event(1, "agent_turn", at(11, 0, 30)), event(2, "scout_report", at(11, 32))],
  thread: [],
};

const PASS_THREE = at(15, 0);
const passTask = (question: string, pass: number, phase: ScoutTask["phase"], started: number, ended: number | null) => ({
  ...servedTask!, question, pass, wave: 1, phase, route: "luna", mode: "read" as const, tokens: null,
  started_ms: started, ended_ms: ended, duration_ms: ended == null ? null : ended - started,
});
const passReport = (pass: number, when: number) => ({ ...servedPass, pass, at: when, report: { ...servedPass.report, summary: `Pass ${pass} report.` } });

/** A scout swarming in pass 3 after its first run and pass 2, as the passes tests build it. */
const swarming: ScoutDetail = {
  ...served,
  scout: {
    ...served.scout, id: 31, kind: "bug", phase: "swarming", reason: null, lead: "opus", helper: "luna", swarm: 4,
    created_ms: at(14, 1), started_ms: at(14, 2), ended_ms: at(14, 35), budget: { ...served.scout.budget, minutes: 60, waves: 1 },
  },
  tasks: [
    passTask("Where does the loop open SQLite connections?", 1, "done", at(14, 3), at(14, 10)),
    passTask("Does the scheduled digest take the write lock?", 2, "done", at(14, 44, 30), at(14, 48)),
    passTask("Does the nightly export hold the lock too?", 3, "done", at(15, 0, 30), at(15, 2)),
    passTask("Does the retry loop reopen its connection?", 3, "running", at(15, 1), null),
  ],
  reports: [passReport(1, at(14, 35)), passReport(2, at(14, 51, 10))],
  actions: [1, 2, 3].map((position) => ({ ...servedAction, position, pass: 1 })),
  events: [event(1, "scout_pass_started", at(14, 44, 5)), event(2, "scout_pass_done", at(14, 51, 15)), event(3, "scout_pass_started", PASS_THREE)],
  thread: [],
};

const listRow: ScoutRow = { ...servedScouts.scouts[0]!, id: 31, title: TITLE, ask: ASK };

/** DEMO answering `/scouts/31` with `scout` and `/scouts` with scout 31's row; every POST is kept. */
function scoutFetch(scout: ScoutDetail): Fetch {
  const polls = fleet({ items: [] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/scouts/31`) return Response.json(scout);
    if (url === `${DEMO}/scouts`) return Response.json({ ...servedScouts, scouts: [listRow] });
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: true });
  };
}

async function mount(hash: string, fetch: Fetch, now = ENDED + 10 * 60_000) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = now;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  for (let round = 0; round < 4; round += 1) await act(settle);
};

const region = (name: string, inside: HTMLElement = document.body) => within(inside).getByRole("region", { name });
const inOrder = (elements: HTMLElement[]) =>
  elements.every((element, index) => index === 0 || (elements[index - 1]!.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0);
const BODY = ["Verdict", "Action items", "Findings", "Progress", "Thread"];
const groups = () => Array.from(screen.getByRole("list", { name: "Scout activity" }).children) as HTMLElement[];
const groupNamed = (title: string) => groups().find((group) => group.querySelector("[data-group-title] b")!.textContent === title)!;
const fold = (group: HTMLElement) => group.querySelector(":scope > div > button[aria-expanded]");
const findingRows = () => within(screen.getByRole("list", { name: "Findings" })).getAllByRole("listitem").filter((li) => li.hasAttribute("data-scout-finding"));
const taskRows = (group: HTMLElement) => Array.from(group.querySelectorAll<HTMLElement>("li[data-helper]"));

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

test("a done scout's header reads its title, a what line and a cost line, keeps its ask behind Ask, and has no Stage", async () => {
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(TITLE);
  expect(document.querySelector("[data-scout-what]")!.textContent).toBe(WHAT);
  expect(document.querySelector("[data-scout-cost]")!.textContent).toBe(COST);
  const ask = within(document.querySelector("header")!).getByRole("button", { name: "Ask" });
  expect(ask.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText(ASK)).toBeNull();
  expect(screen.queryByRole("group", { name: "Stage" })).toBeNull();
  expect(document.body.textContent).not.toContain("Stage");
  await click(ask);
  expect(ask.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByText(ASK)).toBeTruthy();
});

test("the Verdict reads the newest report's headline over its bullets, or its summary with no list when it has no headline", async () => {
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  const verdict = region("Verdict");
  expect(within(verdict).getByRole("status").textContent).toBe(HEADLINE);
  expect(within(within(verdict).getByRole("list")).getAllByRole("listitem").map((li) => li.textContent)).toEqual(BULLETS);
  cleanup();
  await mount("#/scouts/demo/31", scoutFetch(summaryOnly));
  expect(within(region("Verdict")).getByRole("status").textContent).toBe(SUMMARY);
  expect(within(region("Verdict")).queryByRole("list")).toBeNull();
});

test("a done scout's body reads Verdict, Action items, Findings, Progress and Thread; a live pass puts Progress second with its pass open", async () => {
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  expect(inOrder(BODY.map((name) => region(name, region("scout 31"))))).toBe(true);
  cleanup();
  await mount("#/scouts/demo/31", scoutFetch(swarming), PASS_THREE + 3 * 60_000);
  expect(inOrder(["Verdict", "Progress", "Action items", "Findings"].map((name) => region(name)))).toBe(true);
  expect(fold(groupNamed("Pass 3"))!.getAttribute("aria-expanded")).toBe("true");
});

test("a finding's citations stay folded behind a count until pressed", async () => {
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  const [first, second] = findingRows();
  const toggle = within(first!).getByRole("button", { name: "5 citations" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(places.filter((place) => within(first!).queryByText(place) != null)).toEqual([]);
  await click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(places.filter((place) => within(first!).queryByText(place) != null)).toEqual(places);
  expect(within(second!).getByRole("button", { name: "1 citation" })).toBeTruthy();
});

test("the reproduction folds its hypotheses into one counted line, and a report with no reproduction has none", async () => {
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  const line = screen.getByRole("button", { name: "not reproduced · 5 hypotheses · 2 ruled out · 3 open" });
  expect(screen.queryByRole("list", { name: "Hypotheses" })).toBeNull();
  await click(line);
  expect(within(screen.getByRole("list", { name: "Hypotheses" })).getAllByRole("listitem")).toHaveLength(5);
  cleanup();
  await mount("#/scouts/demo/31", scoutFetch(research));
  expect(screen.queryByRole("button", { name: /hypothes/ })).toBeNull();
  expect(region("Findings").querySelector("[data-reproduction]")).toBeNull();
});

test("Progress lists one closed row per group with only the waves folding and no steps, and a wave counts its failed helpers in the bad tone", async () => {
  await mount("#/scouts/demo/31", scoutFetch(twoWaves));
  expect(groups().map((group) => group.querySelector("[data-group-title] b")!.textContent)).toEqual(["Plan", "Wave 1", "Synthesis", "Wave 2", "Report"]);
  expect(groups().map((group) => fold(group)?.getAttribute("aria-expanded") ?? null)).toEqual([null, "false", null, "false", null]);
  expect(document.querySelectorAll("[data-step-text]")).toHaveLength(0);
  cleanup();
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  expect(within(region("Progress")).getByRole("heading").textContent).toBe("Progress3m 51s");
  const sub = groupNamed("Wave 1").querySelector("[data-group-title]")!;
  expect(sub.lastElementChild!.hasAttribute("data-failed")).toBe(true);
  expect(sub.lastElementChild!.textContent).toBe("7 failed");
  cleanup();
  const stoppedToo = { ...scout31, tasks: scout31.tasks.map((each, index) => (index === 4 ? { ...each, phase: "stopped" as const } : each)) };
  await mount("#/scouts/demo/31", scoutFetch(stoppedToo));
  expect(groupNamed("Wave 1").querySelector("[data-badge]")!.getAttribute("data-badge")).toBe("bad");
});

test("an opened wave's task rows read title, a failed one's reason and a meta line, and a pressed row shows its question", async () => {
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  await click(fold(groupNamed("Wave 1"))!);
  const rows = taskRows(groupNamed("Wave 1"));
  const read = (li: HTMLElement) => [
    li.querySelector("[data-task-title]")!.textContent,
    li.querySelector("[data-task-reason]")?.textContent ?? null,
    li.querySelector("[data-task-meta]")!.textContent,
  ];
  expect(rows.filter((li) => li.getAttribute("data-helper") === "failed").map(read)).toEqual(
    [1, 2, 3, 4, 7, 10, 11].map((index) => [`Helper ${index}`, REASON, "run · luna · 0s"]),
  );
  expect(read(rows[4]!)).toEqual(["Helper 5", null, "read · luna · 31s · 37k tok"]);
  const press = within(rows[4]!).getAllByRole("button")[0]!;
  expect(rows[4]!.querySelector("[data-question]")).toBeNull();
  await click(press);
  expect(press.getAttribute("aria-expanded")).toBe("true");
  expect(rows[4]!.querySelector("[data-question]")!.textContent).toBe("Question 5 about tests/loop_fixture.py");
});

test("the Thread ends the body with its composer and no lead or compose line, and Steer renames the box", async () => {
  await mount("#/scouts/demo/31", scoutFetch(scout31));
  const body = document.querySelector<HTMLElement>("[data-scout-body]")!;
  const thread = region("Thread");
  expect(within(body).getAllByRole("region").at(-1)).toBe(thread);
  expect(within(thread).getByRole("textbox", { name: "Question for the scout" })).toBeTruthy();
  expect(document.querySelector("[data-thread-lead], [data-compose-line]")).toBeNull();
  expect(document.body.textContent).not.toContain("Answers from the report.");
  expect(document.body.textContent).not.toContain("lead · opus");
  await click(within(thread).getByRole("button", { name: "Steer" }));
  expect(within(thread).getByRole("textbox", { name: "Note to the scout" })).toBeTruthy();
});

test("the Scout, Run and Turn pages wait for their first answer behind a wordless busy status", async () => {
  const never: Fetch = () => new Promise<Response>(() => {});
  for (const hash of ["#/scouts/demo/1", "#/runs/demo/12", "#/runs/demo/12/turns/42"]) {
    await mount(hash, never);
    const main = screen.getByRole("main");
    const placeholder = within(main).getByRole("status");
    expect([placeholder.getAttribute("aria-busy"), placeholder.textContent]).toEqual(["true", ""]);
    expect(main.textContent).not.toMatch(/Asking demo/);
    cleanup();
  }
});

test("the wide Scouts page's row reads the title and its pane holds the page's header and body", async () => {
  const media = spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches: query === "(min-width: 1400px)",
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as MediaQueryList);
  try {
    await mount("#/scouts", scoutFetch(scout31));
    const row = within(screen.getByRole("list", { name: "Scouts, newest first" })).getByRole("link");
    expect(row.querySelector("[data-cell='title']")!.textContent).toBe(TITLE);
    const pane = region("scout 31");
    expect(within(pane).getAllByRole("heading", { level: 2 })[0]!.textContent).toBe(TITLE);
    expect([pane.querySelector("[data-scout-what]")!.textContent, pane.querySelector("[data-scout-cost]")!.textContent]).toEqual([WHAT, COST]);
    expect(inOrder(BODY.map((name) => region(name, pane)))).toBe(true);
  } finally {
    media.mockRestore();
  }
});
