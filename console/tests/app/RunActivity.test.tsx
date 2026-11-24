import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
const rows = () => Array.from(list().querySelectorAll<HTMLElement>(':scope > [data-slot="timeline-item"]'));
const row = (item: Element) => [item.querySelector("[data-group-title]")!.textContent, item.querySelector("[data-group-span]")!.textContent];
const rowOf = (title: string) => rows().find((item) => item.querySelector("[data-group-title]")!.textContent!.startsWith(title))!;
const steps = (item: Element) => Array.from(item.querySelectorAll("[data-step-text]"), (step) => step.textContent);
const fold = (item: Element) => within(item as HTMLElement).getAllByRole("button")[0]!;
const toggle = async (item: Element) => act(async () => void fireEvent.click(fold(item)));
const shut = async (item: Element) => {
  if (fold(item).getAttribute("aria-expanded") === "true") await toggle(item);
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
  expect(rowOf("Final check").getAttribute("data-current")).toBe("ok");
  expect(rowOf("Final check").getAttribute("aria-current")).toBeNull();
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
  expect(running.getAttribute("data-current")).toBe("live");
  expect(row(running)[1]).toBe("now");
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

const TINT = /(^|-)wash$|^bg-paper-2$|^shadow-\[inset/;
const tints = (el: Element) => Array.from(el.classList).filter((name) => TINT.test(name));
const badges = () => rows().map((li) => li.querySelector<HTMLElement>("[data-badge]")!);
const titleInk = (li: Element) => Array.from(li.querySelector("[data-group-title] b")!.classList).filter((name) => name.startsWith("text-"));

test("a reviewing run's badges are bare glyphs in their tone's text colour, the running one holding the running dot", async () => {
  await mount(runFetch({ detail: reviewing }));
  const ink = (badge: HTMLElement) => Array.from(badge.classList).filter((name) => name.startsWith("text-"));
  expect(badges().map((badge) => [badge.dataset.badge, ink(badge)])).toEqual([
    ["ok", ["text-live"]],
    ["warn", ["text-warn-ink"]],
    ["live", ["text-live"]],
  ]);
  for (const badge of badges()) {
    expect(tints(badge)).toEqual([]);
    expect(Array.from(badge.classList).filter((name) => name === "rounded-md" || name === "size-[30px]")).toEqual([]);
  }
  const holds = (badge: HTMLElement) => badge.querySelector("[data-state]")?.getAttribute("data-state") ?? (badge.querySelector(":scope > svg") == null ? null : "glyph");
  expect(badges().map(holds)).toEqual(["glyph", "glyph", "running"]);
});

test("a live run's running group alone is current, untinted, with its title in full ink and earlier titles in ink-2", async () => {
  await mount(runFetch({ detail: reviewing }));
  expect(rows().map((li) => li.getAttribute("data-current"))).toEqual([null, null, "live"]);
  expect(rows().map(tints)).toEqual([[], [], []]);
  expect(rows().map(titleInk)).toEqual([["text-ink-2"], ["text-ink-2"], ["text-ink"]]);
});

test("a reviewing run's Progress is one Run activity list of groups named by title, described by time and duration, with one roving tab stop", async () => {
  await mount(runFetch({ detail: reviewing }));
  expect(within(progress()).getAllByRole("list", { name: "Run activity" })).toHaveLength(1);
  const items = rows();
  expect(Array.from(list().children)).toEqual(items);
  expect(items.map((item) => item.getAttribute("role"))).toEqual(["listitem", "listitem", "listitem"]);
  expect(items.map((item) => item.getAttribute("tabindex"))).toEqual(["0", "-1", "-1"]);
  expect(items.every((item) => item.getAttribute("aria-current") === null)).toBe(true);
  expect(within(list()).getByRole("listitem", { name: "Implement", description: "10:00 10m 00s" })).toBe(items[0]!);
  expect(within(list()).getByRole("listitem", { name: "Round 1 · 3 should-fix", description: "10:10 5m 30s" })).toBe(items[1]!);
  expect(within(list()).getByRole("listitem", { name: "Round 2", description: "10:15 now" })).toBe(items[2]!);
  expect(items[2]!.querySelector('[data-state="running"]')!.closest('[aria-hidden="true"]')).not.toBeNull();
});

test("ArrowDown, End, Home and wrapping ArrowUp move focus across Progress groups, and Tab moves from a focused group to its fold button", async () => {
  const user = userEvent.setup();
  await mount(runFetch({ detail: reviewing }));
  const items = rows();
  await act(async () => void items[0]!.focus());
  expect(document.activeElement).toBe(items[0]);

  await user.keyboard("{ArrowDown}");
  expect(document.activeElement).toBe(items[1]);
  expect(items.map((item) => item.getAttribute("tabindex"))).toEqual(["-1", "0", "-1"]);

  await user.tab();
  expect(document.activeElement).toBe(fold(items[1]!));

  await user.tab({ shift: true });
  expect(document.activeElement).toBe(items[1]);

  await user.keyboard("{End}");
  expect(document.activeElement).toBe(items[2]);

  await user.keyboard("{Home}");
  expect(document.activeElement).toBe(items[0]);

  await user.keyboard("{ArrowUp}");
  expect(document.activeElement).toBe(items[2]);

  await user.tab();
  expect(document.activeElement).toBe(fold(items[2]!));
});

async function waitInert(element: HTMLElement) {
  for (let attempt = 0; attempt < 25 && element.closest("[inert]") == null; attempt += 1) {
    await act(() => new Promise<void>((resolve) => setTimeout(resolve, 10)));
  }
  expect(element.closest("[inert]")).not.toBeNull();
}

test("Raw log opens a dialog of every Progress line, heartbeats included, and Escape returns focus to its button", async () => {
  await mount(runFetch({ detail: ended }));
  const button = within(progress()).getByRole("button", { name: /^Raw log/ });
  await act(async () => void fireEvent.click(button));
  const dialog = screen.getByRole("dialog", { name: "Raw log · run 12" });
  expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Close raw log" }));
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
  await waitInert(button);
  await act(async () => void fireEvent.keyDown(document, { key: "Escape" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(button.closest("[inert]")).toBeNull();
  expect(document.activeElement).toBe(button);
});

test("Close raw log and pressing the dimmed page beside Raw log each close the panel and return focus to its button", async () => {
  await mount(runFetch({ detail: ended }));
  const button = within(progress()).getByRole("button", { name: /^Raw log/ });
  for (const close of [
    (closeButton: HTMLElement) => fireEvent.click(closeButton),
    () => {
      const backdrop = document.querySelector<HTMLElement>('[data-slot="dialog-backdrop"]');
      expect(backdrop).toBeTruthy();
      fireEvent.pointerDown(backdrop!, { button: 0 });
      fireEvent.pointerUp(backdrop!, { button: 0 });
      fireEvent.click(backdrop!);
    },
  ]) {
    await act(async () => void fireEvent.click(button));
    const closeButton = within(screen.getByRole("dialog", { name: "Raw log · run 12" })).getByRole("button", { name: "Close raw log" });
    expect(document.activeElement).toBe(closeButton);
    await waitInert(button);
    await act(async () => void close(closeButton));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button.closest("[inert]")).toBeNull();
    expect(document.activeElement).toBe(button);
  }
});
