import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Round, RunAsks, RunDetail, RunEvent } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, run, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const [servedRound] = served.rounds;

const T = new Date(2026, 9, 15, 10, 0).getTime();
const MIN = 60_000;
const SEC = 1_000;

const phase = (at: number, summary: string): RunEvent => ({ at, kind: "phase_change", summary });
const beat = (at: number, summary: string): RunEvent => ({ at, kind: "turn_progress", summary });
const p2 = (message: string) => ({ ...servedRound!.findings[0]!, severity: "p2", message });

const round = (fields: Partial<Round>): Round =>
  ({ ...servedRound!, instructions: [], operator_notes: [], criteria: [], findings: [], ...fields }) as Round;

const roundOne = round({
  round: 1,
  started_ms: T + 10 * MIN + 30 * SEC,
  ended_ms: T + 13 * MIN + 30 * SEC,
  verdict: "changes_requested",
  reviewer_model: "codex-sol-high",
  findings: [p2("one"), p2("two"), p2("three")],
});

const EVENTS = [
  phase(T, "claimed -> working"),
  beat(T + 2 * MIN, "Implement turn: 4 tool calls; last: reading a file"),
  beat(T + 6 * MIN, "Implement turn: 9 tool calls; last: running a command"),
  phase(T + 10 * MIN, "working -> verifying"),
  phase(T + 10 * MIN + 30 * SEC, "verifying -> reviewing: round 1 review"),
  phase(T + 13 * MIN + 30 * SEC, "reviewing -> addressing"),
  phase(T + 15 * MIN + 30 * SEC, "addressing -> verifying"),
];

/** Run 12, ended after its implement and a review round of verify 30s, review 3m and fix 2m. */
const ended: RunDetail = {
  ...served,
  run: { ...served.run, id: 12, ticket: "DEMO-12", phase: "done", outcome: "merged", started_ms: T, ended_ms: T + 16 * MIN, max_rounds: 3 },
  events: EVENTS,
  rounds: [roundOne],
  steer_notes: [],
  criteria: [],
  precheck: null,
};

const live: RunDetail = {
  ...ended,
  run: { ...ended.run, phase: "working", outcome: null, ended_ms: null },
  events: EVENTS.slice(0, 3),
  rounds: [],
};

const metC1 = { id: "C1", n: 1, status: "met" as const, note: "" };

/** Run 12, merged after its Round 1 asked for changes and left C1 not met, then passed its final check. */
const checked: RunDetail = {
  ...ended,
  run: { ...ended.run, max_rounds: 1, ended_ms: T + 20 * MIN },
  events: [
    ...EVENTS.slice(0, 3),
    { at: T + 8 * MIN, kind: "rebase", summary: "rebased the candidate on main" },
    ...EVENTS.slice(3),
    phase(T + 16 * MIN, "verifying -> reviewing: terminal adjudication"),
  ],
  rounds: [
    { ...roundOne, criteria: [{ ...metC1, status: "not_met", note: "the export is absent" }] },
    round({ round: 2, started_ms: T + 16 * MIN + SEC, ended_ms: T + 18 * MIN, verdict: "pass", reviewer_model: "claude-adjudicate", criteria: [metC1] }),
  ],
};

/** Run 12, live in the verify after Round 1's fix. */
const reviewing: RunDetail = {
  ...live,
  events: [...EVENTS, beat(T + 15 * MIN + 45 * SEC, "Implement turn: 9 tool calls; last: running a command")],
  rounds: [roundOne],
};

const noAsks: RunAsks = { run: 12, ticket: "DEMO-12", pr_url: null, asks: [] };

/** `/runs/12` answers `bodies.detail`, read at each request; run 12 is live in `/status` while it has no outcome. */
function runFetch(bodies: { detail: RunDetail }): Fetch {
  const working = fleet({ items: [], runs: [{ ...run(12, "DEMO-12"), phase: "working", round: 0, max_rounds: 3 }] }, { items: [] });
  const idle = fleet({ items: [] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/runs/12`) return Response.json(bodies.detail);
    if (url === `${DEMO}/runs/12/asks`) return Response.json(noAsks);
    return (bodies.detail.run.outcome == null ? working : idle)(url);
  };
}

async function mount(fetch: Fetch) {
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return queries;
}

async function repoll(queries: ReturnType<typeof consoleQueryClient>) {
  await act(async () => {
    await queries.refetchQueries();
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
}

const progress = () => screen.getByRole("region", { name: "Progress" });
const list = () => within(progress()).getByRole("list", { name: "Run activity" });
const rows = () => Array.from(list().querySelectorAll(":scope > li"));
const row = (li: Element) => [li.querySelector("[data-group-title]")!.textContent, li.querySelector("[data-group-span]")!.textContent];
const rowOf = (title: string) => rows().find((li) => li.querySelector("[data-group-title]")!.textContent!.startsWith(title))!;
const steps = (li: Element) => Array.from(li.querySelectorAll("[data-step-text]"), (step) => step.textContent);
const fold = (li: Element) => within(li as HTMLElement).getAllByRole("button")[0]!;
const toggle = async (li: Element) => act(async () => void fireEvent.click(fold(li)));
const shut = async (li: Element) => {
  if (fold(li).getAttribute("aria-expanded") === "true") await toggle(li);
};

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

test("Progress lists implement and the review round as timed groups, with no heartbeat outside the Raw log", async () => {
  await mount(runFetch({ detail: ended }));
  expect(rows().map(row)).toEqual([
    ["Implement", "10m 00s"],
    ["Round 1 · 3 should-fix", "5m 30s"],
  ]);
  for (const fold of within(list()).queryAllByRole("button", { expanded: false })) await act(async () => void fireEvent.click(fold));
  expect(document.body.textContent).not.toContain("Implement turn:");
});

test("an open round reads its verify, review and fix with their durations, and a verify under a second reads <1s", async () => {
  await mount(runFetch({ detail: ended }));
  const first = rowOf("Round 1");
  await shut(first);
  await toggle(first);
  expect(steps(first)).toEqual(["Verify 30s", "Review 3m 00s · codex-sol-high", "Fix 2m 00s"]);

  cleanup();
  const second = T + 15 * MIN + 30 * SEC;
  await mount(runFetch({
    detail: {
      ...ended,
      events: [...EVENTS, phase(second + 400, "verifying -> reviewing: round 2 review")],
      rounds: [roundOne, round({ round: 2, started_ms: second + 400, ended_ms: second + MIN, verdict: "pass" })],
    },
  }));
  expect(steps(rowOf("Round 2"))[0]).toBe("Verify <1s");
});

const current = () => rows().filter((li) => li.hasAttribute("data-current"));
const body = (li: Element) => document.getElementById(fold(li).getAttribute("aria-controls")!);

test("an ended run opens only its last group, the current one, and a last group the reader closed stays closed when the next poll adds an event", async () => {
  const bodies = { detail: checked };
  const queries = await mount(runFetch(bodies));
  expect(rows().map((li) => [row(li)[0]!.split(" · ")[0], fold(li).getAttribute("aria-expanded")])).toEqual([
    ["Implement", "false"],
    ["Round 1", "false"],
    ["Final check", "true"],
  ]);
  expect(current()).toEqual([rowOf("Final check")]);
  await toggle(rowOf("Final check"));
  bodies.detail = { ...checked, events: [...checked.events, { at: T + 19 * MIN, kind: "trim", summary: "Trim kept; line delta -2" }] };
  await repoll(queries);
  expect(fold(rowOf("Final check")).getAttribute("aria-expanded")).toBe("false");
  expect(steps(rowOf("Final check"))).toEqual([]);
});

test("a live run has no Now region; its running group alone is open and current, its body ending on the newest progress, replaced as a newer one arrives", async () => {
  const bodies = { detail: reviewing };
  const queries = await mount(runFetch(bodies));
  expect(within(progress()).queryByRole("region", { name: "Now" })).toBeNull();
  const running = rows().at(-1)!;
  expect(running.hasAttribute("data-running")).toBe(true);
  expect(current()).toEqual([running]);
  expect(rows().map((li) => li.querySelector("button[aria-expanded]")?.getAttribute("aria-expanded") ?? null)).toEqual([null, "false", "true"]);
  expect(body(running)!.textContent).toContain("Implement turn: 9 tool calls; last: running a command");
  const before = body(running)!.childElementCount;

  bodies.detail = { ...reviewing, events: [...reviewing.events, beat(T + 16 * MIN, "Implement turn: 12 tool calls; last: editing a file")] };
  await repoll(queries);
  const after = body(rows().at(-1)!)!;
  expect(after.textContent).toContain("Implement turn: 12 tool calls; last: editing a file");
  expect(after.textContent).not.toContain("Implement turn: 9 tool calls");
  expect(after.childElementCount).toBe(before);
  expect(rows()).toHaveLength(3);
});

test("Raw log opens a dialog of every Progress line, heartbeats included, and Escape returns focus to its button", async () => {
  await mount(runFetch({ detail: ended }));
  const button = within(progress()).getByRole("button", { name: /^Raw log/ });
  await act(async () => void fireEvent.click(button));
  const dialog = screen.getByRole("dialog", { name: "Raw log · run 12" });
  const lines = Array.from(dialog.querySelectorAll("li"), (li) => `${li.querySelector("time")!.textContent} ${li.querySelector("[data-line-text]")!.textContent}`);
  expect(lines).toEqual([
    "10:00 Run started · implementing",
    "10:02 Implement turn: 4 tool calls; last: reading a file",
    "10:06 Implement turn: 9 tool calls; last: running a command",
    "10:10 Implement done · verifying",
    "10:10 Review round 1 started",
    "10:13 Review round 1: changes requested, 0 must-fix",
    "10:13 Back to implement",
    "10:15 Fix applied · verifying",
    "10:16 Merged to main",
  ]);
  expect(button.textContent).toContain(String(lines.length));
  await act(async () => void fireEvent.keyDown(document, { key: "Escape" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(button);
});
