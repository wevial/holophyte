import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, RunDetail, RunTurns, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const changes = servedRun.rounds.find((round) => round.verdict === "changes_requested")!;
type Round = RunDetail["rounds"][number];
type Turn = RunTurns["turns"][number];

const STARTED = new Date(2026, 9, 15, 11, 30).getTime();
const detail = (id: number, ticket: string, facts: Partial<RunDetail["run"]>, rounds: RunDetail["rounds"] = []): RunDetail => ({
  ...servedRun,
  rounds,
  run: { ...servedRun.run, id, ticket, title: `${ticket} title`, host: "writer-1", started_ms: STARTED, ...facts },
});

const fixing = (id: number, after: RunDetail["events"]): RunDetail => ({
  ...detail(id, `DEMO-${id}`, { phase: "merge_gate" }),
  events: [
    { at: STARTED, kind: "babysit_step", summary: "checks" },
    { at: STARTED + 1, kind: "check_fix_round", summary: "check fix round 1 of 2 on https://github.com/o/r/pull/7: vitest; failing: 2 tests red" },
    { at: STARTED + 2, kind: "babysit_step", summary: "fix" },
    ...after,
  ],
});

const reviewRound = (round: number, verdict: string, startMin: number, endMin: number | null, findings: Round["findings"] = []): Round => ({
  ...changes,
  round,
  verdict,
  findings,
  reviewer_model: "gpt-6-sol",
  started_ms: STARTED + startMin * MINUTE,
  ended_ms: endMin == null ? null : STARTED + endMin * MINUTE,
});
const phaseChange = (minute: number, summary: string) => ({ at: STARTED + minute * MINUTE, kind: "phase_change", summary });
const turn = (id: number, role: Turn["role"], label: string | null, endMin: number): Turn => ({
  id, role, label, route: "primary", seconds: 60, session_id: null, ended_ms: STARTED + endMin * MINUTE,
  exit_status: 0, timed_out: false, input_tokens: null, output_tokens: null, cost_usd: null,
});

const roundOne = reviewRound(1, "changes_requested", 10, 12, [
  { path: "holophyte/serve/routes.py", line: 4, severity: "p1", title: "The template route is unmatched", message: "unmatched" },
  { path: "holophyte/serve/routes.py", line: 9, severity: "p2", message: "a nit" },
]);
const roundTwo = reviewRound(2, "changes_requested", 20, 22);
const passed = reviewRound(3, "pass", 30, 32);

const turnsOf: Record<number, Turn[]> = {
  51: [turn(1, "implement", null, 5), turn(2, "review", null, 12), turn(3, "implement", "fix round 1", 15), turn(4, "implement", "witness", 17)],
};

const runs: Record<number, RunDetail> = {
  12: detail(12, "DEMO-12", { title: "Count the rounds", phase: "reviewing", agent_ms: 16 * MINUTE, time_box_ms: 25 * MINUTE, max_rounds: 3 }, [changes]),
  50: detail(50, "DEMO-50", { phase: "verifying", max_rounds: 6 }),
  51: detail(51, "DEMO-51", { phase: "reviewing", max_rounds: 6 }, [roundOne, roundTwo, reviewRound(3, "", 30, null)]),
  53: {
    ...detail(53, "DEMO-53", { phase: "reviewing", max_rounds: 6 }, [roundOne, roundTwo]),
    events: [phaseChange(22, "reviewing -> addressing"), phaseChange(26, "addressing -> verifying"), phaseChange(30, "verifying -> reviewing: round 3 review")],
  },
  54: {
    ...detail(54, "DEMO-54", { phase: "merge_gate", max_rounds: 1 }, [roundOne, { ...passed, round: 2 }, reviewRound(3, "changes_requested", 50, 52)]),
    events: [{ at: STARTED + 45 * MINUTE, kind: "babysit_step", summary: "threads" }],
  },
  55: {
    ...detail(55, "DEMO-55", { phase: "reviewing", max_rounds: 6 }, [{ ...passed, round: 1 }]),
    events: [
      { at: STARTED + 40 * MINUTE, kind: "babysit_step", summary: "covering_review" },
      phaseChange(40, "merge_gate -> reviewing: review of the fix at 0123456789ab"),
    ],
  },
  56: {
    ...detail(56, "DEMO-56", { phase: "merge_gate", max_rounds: 3 }, [{ ...passed, round: 1 }, reviewRound(2, "pass", 41, 43), reviewRound(3, "pass", 46, 48), reviewRound(4, "pass", 51, 53)]),
    events: [{ at: STARTED + 40 * MINUTE, kind: "babysit_step", summary: "covering_review" }],
  },
  57: {
    ...detail(57, "DEMO-57", { phase: "reviewing", max_rounds: 3 }, [{ ...passed, round: 1 }, reviewRound(2, "approved", 41, 43)]),
    events: [
      { at: STARTED + 40 * MINUTE, kind: "babysit_step", summary: "covering_review" },
      phaseChange(40, "merge_gate -> reviewing: review of the fix at 0123456789ab"),
    ],
  },
  52: {
    ...detail(52, "DEMO-52", { phase: "merge_gate", max_rounds: 6 }, [roundOne, roundTwo, passed]),
    events: [
      { at: STARTED + 40 * MINUTE, kind: "babysit_step", summary: "checks" },
      { at: STARTED + 41 * MINUTE, kind: "check_fix_round", summary: "check fix round 1 of 3 on https://github.com/o/r/pull/7: lint; failing: E501 line too long" },
      { at: STARTED + 42 * MINUTE, kind: "babysit_step", summary: "fix" },
      { at: STARTED + 45 * MINUTE, kind: "babysit_step", summary: "checks" },
      { at: STARTED + 46 * MINUTE, kind: "check_fix_round", summary: "check fix round 2 of 3 on https://github.com/o/r/pull/7: unit; failing: 2 tests red" },
      { at: STARTED + 47 * MINUTE, kind: "babysit_step", summary: "fix" },
    ],
  },
  13: fixing(13, []),
  15: fixing(15, [{ at: STARTED + 3, kind: "babysit_step", summary: "checks" }]),
  16: fixing(16, [
    { at: STARTED + 3, kind: "babysit_step", summary: "checks" },
    { at: STARTED + 4, kind: "check_fix_round", summary: "check fix round 2 of 2 on https://github.com/o/r/pull/7: vitest, lint; failing: 2 tests red; E501 line too long" },
    { at: STARTED + 5, kind: "babysit_step", summary: "fix" },
  ]),
  17: fixing(17, [
    { at: STARTED + 3, kind: "check_fix_round", summary: "check fix round 2 of 2 on https://github.com/o/r/pull/7: vitest, lint; failing: 2 tests red; E501 line too long" },
    { at: STARTED + 3, kind: "babysit_step", summary: "checks" },
    { at: STARTED + 4, kind: "babysit_step", summary: "fix" },
  ]),
  14: detail(14, "DEMO-14", { title: "Read a `+++` TOML header" }),
  40: {
    ...detail(40, "DEMO-40", {
      phase: "failed",
      outcome: "failed",
      failure_kind: "verify",
      outcome_reason: "2 tests red in tests/test_board_cache.py\nmore",
      ended_ms: NOW - 10 * MINUTE,
    }),
    events: [phaseChange(0, "claimed -> working"), phaseChange(5, "working -> verifying"), phaseChange(8, "verifying -> failed: 2 tests red")],
  },
  9: detail(9, "DEMO-9", {
    phase: "done",
    outcome: "merged",
    max_rounds: 6,
    merge_sha: "5acc1384b2e0f6a7c9d8e1f203a4b5c6d7e8f901",
    ended_ms: new Date(2026, 9, 15, 11, 15).getTime(),
  }, [roundOne, { ...passed, round: 2 }]),
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
    const [, id, turns] = /^\/runs\/(\d+)(\/turns)?$/.exec(url.slice(DEMO.length)) ?? [];
    if (url.startsWith(DEMO) && id != null) {
      const answer = turns ? { turns: turnsOf[Number(id)] ?? [] } : runs[Number(id)];
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
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const verdict = () => within(screen.getByRole("main")).getByRole("status").textContent;
const stages = () => within(screen.getByRole("list", { name: "Stages" })).getAllByRole("listitem").map((item) => item.getAttribute("aria-label"));
const marks = (name: string) => [...screen.getByRole("group", { name }).querySelectorAll("[aria-label]")];
const markNames = (name: string) => marks(name).map((mark) => mark.getAttribute("aria-label"));
const tooltip = () => screen.queryByRole("tooltip");

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

test("a reviewing run's breadcrumb names its ticket and run, its verdict says where it stands and its stepper shows its review round", async () => {
  await mount("#/runs/demo/12");
  const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
  expect(crumb.textContent).toBe("Runs / demo tickets / DEMO-12 / run 12");
  expect(within(crumb).getByRole("link", { name: "Runs" }).getAttribute("href")).toEndWith("#/runs");
  expect(within(crumb).getByRole("link", { name: "demo tickets" }).getAttribute("href")).toEndWith("#/tickets?project=demo");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Count the rounds");
  expect(verdict()).toBe("Reviewing round 2 of 3. None of one criterion is met yet.");
  expect(stages()).toContain("Review · round 2 / 3: current");
});

test("a reviewing run's facts row gives its routes, start and host as named icons with values, each naming itself on keyboard focus", async () => {
  await mount("#/runs/demo/12");
  const icons = within(document.querySelector<HTMLElement>("[data-run-facts-row]")!).getAllByRole("img");
  expect(icons.map((icon) => [icon.getAttribute("aria-label"), icon.parentElement!.textContent])).toEqual([
    ["Implementer", "claude opus"],
    ["Reviewer", "codex gpt-6-sol"],
    ["Started", "11:30 PDT"],
    ["Host", "writer-1"],
  ]);
  for (const icon of icons) {
    await act(async () => {
      fireEvent.keyDown(document.body, { key: "Tab" });
      icon.parentElement!.focus();
    });
    expect(tooltip()?.textContent).toBe(icon.getAttribute("aria-label") ?? "");
    await act(async () => icon.parentElement!.blur());
  }
  expect(document.querySelector("[data-run-facts]")).toBeNull();
});

test("the verdict line follows its sentence with the agent time against its box, the bar filled to its share and the box named on focus", async () => {
  await mount("#/runs/demo/12");
  const line = document.querySelector<HTMLElement>("[data-run-verdict]")!;
  expect(line.textContent).toBe("Reviewing round 2 of 3. None of one criterion is met yet.16m / 25m");
  const fill = line.querySelector<HTMLElement>("[data-time-fill]")!;
  expect(fill.style.width).toBe("64%");
  await act(async () => {
    fireEvent.keyDown(document.body, { key: "Tab" });
    fill.parentElement!.parentElement!.focus();
  });
  expect(tooltip()?.textContent).toBe("16m of a 25m box");
});

test("the run body sits on the canvas with no card, and Progress is a sentence-case head", async () => {
  await mount("#/runs/demo/12");
  const body = document.querySelector("[data-run-body]")!;
  expect(body.className).not.toMatch(/\bborder\b|\bbg-paper\b/);
  const progress = within(screen.getByRole("region", { name: "Progress" })).getByRole("heading", { level: 2 });
  expect(progress.firstChild!.textContent).toBe("Progress");
  expect(progress.className).not.toMatch(/uppercase|tracking-/);
});

test("neither a reviewing run nor a merged run says Nothing needs you; a failed run waiting in Needs you gives its failure and reason", async () => {
  await mount("#/runs/demo/12");
  expect(document.body.textContent).not.toContain("Nothing needs you");
  cleanup();
  await mount("#/runs/demo/40");
  expect(verdict()).toBe("Failed at verify: 2 tests red in tests/test_board_cache.py.");
  cleanup();
  await mount("#/runs/demo/9");
  expect(verdict()).toBe("Merged to main as 5acc138 at 11:15.");
  expect(document.body.textContent).not.toContain("Nothing needs you");
});

test("neither a live run's page nor a merged run's has a phase timeline", async () => {
  await mount("#/runs/demo/12");
  expect(within(screen.getByRole("region", { name: "Progress" })).getByRole("list", { name: "Run activity" })).toBeTruthy();
  expect(screen.queryByRole("list", { name: "Phase timeline" })).toBeNull();
  cleanup();
  await mount("#/runs/demo/9");
  expect(within(screen.getByRole("region", { name: "Progress" })).getByRole("list", { name: "Run activity" })).toBeTruthy();
  expect(screen.queryByRole("list", { name: "Phase timeline" })).toBeNull();
});

test("a run in its merge phase or its merge gate with no check fix round reads as merging in the Merge gate stage", async () => {
  await mount("#/runs/demo/12", runFetch({ ...liveRow, phase: "merging" }));
  expect(verdict()).toBe("Merging. None of one criterion is met yet.");
  expect(stages()).toContain("Merge gate: current");
  cleanup();
  await mount("#/runs/demo/12", runFetch({ ...liveRow, phase: "merge_gate" }));
  expect(verdict()).toBe("Merging. None of one criterion is met yet.");
  expect(stages()).toContain("Merge gate: current");
});

test("a run in its merge gate during a check fix round says it is fixing CI and names the failing check and round", async () => {
  await mount("#/runs/demo/13", runFetch({ ...liveRow, id: 13, ticket: "DEMO-13", phase: "merge_gate" }));
  expect(verdict()).toStartWith("Fixing CI: vitest failing (check fix round 1 of 2).");
});

test("a merge gate run whose babysitter moved on from its fix step to checks reads as merging again, its check fix mark no longer live", async () => {
  await mount("#/runs/demo/15", runFetch({ ...liveRow, id: 15, ticket: "DEMO-15", phase: "merge_gate" }));
  expect(verdict()).toStartWith("Merging.");
  expect(marks("Merge gate rounds").map((mark) => mark.getAttribute("data-mark"))).toEqual(["ended"]);
});

test("a later check fix round on two checks names both and its own round, not the earlier one", async () => {
  await mount("#/runs/demo/16", runFetch({ ...liveRow, id: 16, ticket: "DEMO-16", phase: "merge_gate" }));
  expect(verdict()).toStartWith("Fixing CI: vitest, lint failing (check fix round 2 of 2).");
});

test("a check fix round logged before the newest checks step at the same instant is not taken as the fix step's round", async () => {
  await mount("#/runs/demo/17", runFetch({ ...liveRow, id: 17, ticket: "DEMO-17", phase: "merge_gate" }));
  expect(verdict()).toStartWith("Merging.");
});

test("a run past its heartbeat threshold says how long it has been silent and counts its criteria before its daemon raises it", async () => {
  await mount("#/runs/demo/12", runFetch({ ...liveRow, heartbeat_age_ms: 15 * MINUTE }));
  expect(verdict()).toBe("No heartbeat for 15m. It is up in Needs you on the Overview. None of one criterion is met yet.");
});

test("an unknown project and a run the daemon lacks each say so, and a host yet to answer shows a loading status", async () => {
  await mount("#/runs/nowhere/12");
  expect(screen.getByText("No project named nowhere answers this console.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Back to Runs" }).getAttribute("href")).toEndWith("#/runs");
  cleanup();
  await mount("#/runs/demo/999");
  expect(screen.getByText("demo has no run 999.")).toBeTruthy();
  cleanup();
  await mount("#/runs/demo/12", () => new Promise<Response>(() => {}));
  const loading = within(screen.getByRole("main")).getByRole("status", { name: "Loading" });
  expect(loading.getAttribute("aria-busy")).toBe("true");
  expect(loading.textContent).toBe("");
});

test("a run page under another project's scope shows its own project's run and keeps the scope", async () => {
  await mount("#/runs/demo/12?project=other");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Count the rounds");
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("project")).toBe("other");
});

test("a backticked span in the run's title shows as inline code in its heading", async () => {
  await mount("#/runs/demo/14");
  const heading = screen.getByRole("heading", { level: 1 });
  expect([...heading.querySelectorAll("code")].map((code) => code.textContent)).toEqual(["+++"]);
  expect(heading.textContent).toBe("Read a +++ TOML header");
});

const STAGE_NAMES = ["Implement", "Verify", "Review", "Merge gate", "Merged"];

test("a verifying run's stepper has Implement finished, Verify current and the later stages not reached", async () => {
  await mount("#/runs/demo/50");
  expect(stages()).toEqual(["Implement: finished", "Verify: current", "Review: not reached", "Merge gate: not reached", "Merged: not reached"]);
});

test("a run in review round 3 of 6 shows two loop marks, a running mark and three unused slots under Review", async () => {
  await mount("#/runs/demo/51");
  expect(stages()[2]).toBe("Review · round 3 / 6: current");
  const rounds = within(screen.getByRole("group", { name: "Review rounds" }));
  const links = rounds.getAllByRole("link");
  expect(links.map((link) => link.getAttribute("aria-label"))).toEqual(["Round 1: changes requested", "Round 2: changes requested", "Round 3: in review"]);
  expect(links.map((link) => link.getAttribute("data-mark"))).toEqual(["loop", "loop", "running"]);
  expect(links[0]!.getAttribute("href")).toEndWith("#round-1");
  expect(rounds.getAllByRole("img", { name: "unused round" })).toHaveLength(3);
});

test("a reviewing run whose round under way has no row yet shows that round as a running mark, not an unused slot", async () => {
  await mount("#/runs/demo/53");
  expect(stages()[2]).toBe("Review · round 3 / 6: current");
  const rounds = within(screen.getByRole("group", { name: "Review rounds" }));
  expect(rounds.getAllByRole("link").map((link) => link.getAttribute("data-mark"))).toEqual(["loop", "loop", "running"]);
  expect(rounds.getAllByRole("img", { name: "unused round" })).toHaveLength(3);
});

function scrollTarget(mark: Element): Element {
  const scrolled: Element[] = [];
  const scroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); };
  try {
    fireEvent.click(mark);
  } finally {
    Element.prototype.scrollIntoView = scroll;
  }
  expect(scrolled).toHaveLength(1);
  return scrolled[0]!;
}

test("a review mark, its round recorded or still under way, scrolls to its own round in Progress", async () => {
  for (const [run, name, round] of [
    [51, "Round 2: changes requested", "Round 2"],
    [53, "Round 3: in review", "Round 3"],
    [54, "Round 2: pass", "Final check"],
    [54, "Round 3: changes requested", "Review threads"],
  ] as const) {
    await mount(`#/runs/demo/${run}`);
    const target = scrollTarget(screen.getByRole("link", { name }));
    expect(screen.getByRole("region", { name: "Progress" }).contains(target)).toBe(true);
    expect(target.querySelector("[data-group-title]")!.textContent).toStartWith(round);
    cleanup();
  }
});

test("a covering review under way links its mark to its round's covering review in Progress", async () => {
  await mount("#/runs/demo/55");
  const mark = screen.getByRole("link", { name: "Round 2: in review" });
  const target = document.getElementById(mark.getAttribute("href")!.slice(1));
  expect(target?.getAttribute("data-group")).toBe("covering_review");
});

test("a covering review retried twice shows its three rounds under Merge gate, each linked to and scrolling to its own covering review in Progress", async () => {
  await mount("#/runs/demo/56");
  const list = within(screen.getByRole("region", { name: "Progress" })).getByRole("list", { name: "Run activity" });
  const reviewMarks = within(screen.getByRole("group", { name: "Review rounds" })).getAllByRole("link");
  expect(reviewMarks.map((mark) => mark.getAttribute("aria-label"))).toEqual(["Round 1: pass"]);
  expect(reviewMarks[0]!.getAttribute("href")).toBe("#round-1");
  const rechecks = within(screen.getByRole("group", { name: "Merge gate rounds" })).getAllByRole("link");
  expect(rechecks.map((mark) => mark.getAttribute("aria-label"))).toEqual(["Round 2: pass", "Round 3: pass", "Round 4: pass"]);

  const expected = [
    [reviewMarks[0]!, "round-1", "round", "Round 1"],
    [rechecks[0]!, "round-2", "covering_review", "Covering review · round 2"],
    [rechecks[1]!, "round-3", "covering_review", "Covering review · round 3"],
    [rechecks[2]!, "round-4", "covering_review", "Covering review · round 4"],
  ] as const;
  const targets = expected.map(([mark, id, kind, title]) => {
    const target = scrollTarget(mark);
    expect(target).toBe(document.getElementById(id)!);
    expect(target.parentElement).toBe(list);
    expect(target.getAttribute("data-group")).toBe(kind);
    expect(target.querySelector("[data-group-title]")!.textContent).toStartWith(title);
    return target;
  });
  expect(new Set(targets).size).toBe(4);
});

test("an approved covering review still reviewing, with no later review begun, adds no round and keeps Merge gate current", async () => {
  await mount("#/runs/demo/57");
  expect(stages()).toEqual(["Implement: finished", "Verify: finished", "Review · 1 round: finished", "Merge gate: current", "Merged: not reached"]);
  expect(markNames("Review rounds")).toEqual(["Round 1: pass"]);
  expect(markNames("Merge gate rounds")).toEqual(["Round 2: approved"]);
});

test("a review mark's card names its round, verdict, findings and blockers on keyboard focus and goes on blur", async () => {
  await mount("#/runs/demo/51");
  const first = screen.getByRole("link", { name: "Round 1: changes requested" });
  expect(tooltip()).toBeNull();
  await act(async () => {
    fireEvent.keyDown(document.body, { key: "Tab" });
    first.focus();
  });
  const card = tooltip()!;
  expect([...card.children].map((line) => line.textContent)).toEqual([
    "Round 1 · changes requested",
    "2 findings · 1 blocker",
    "The template route is unmatched",
    "gpt-6-sol",
  ]);
  await act(async () => first.blur());
  expect(tooltip()).toBeNull();
});

test("a turn between two review rounds other than the first implement turn after the earlier one is a sub-mark between their marks", async () => {
  await mount("#/runs/demo/51");
  expect(markNames("Review rounds").slice(0, 4)).toEqual(["Round 1: changes requested", "witness", "Round 2: changes requested", "Round 3: in review"]);
  expect(markNames("Review rounds")).not.toContain("fix round 1");
});

test("a merge gate run after three rounds ending in a pass reads Review done, names its babysit round and marks its two check fix rounds, the second live", async () => {
  await mount("#/runs/demo/52");
  expect(stages()).toEqual(["Implement: finished", "Verify: finished", "Review · 3 rounds: finished", "Merge gate · babysit 2: current", "Merged: not reached"]);
  expect(within(screen.getByRole("group", { name: "Review rounds" })).queryAllByRole("img", { name: "unused round" })).toEqual([]);
  const fixes = within(screen.getByRole("group", { name: "Merge gate rounds" })).getAllByRole("button");
  expect(fixes.map((mark) => [mark.getAttribute("aria-label"), mark.getAttribute("data-mark")])).toEqual([
    ["Check fix round 1 of 3", "failed"],
    ["Check fix round 2 of 3", "running"],
  ]);
});

test("a run that failed in verify shows Verify failed and the stages after it not reached", async () => {
  await mount("#/runs/demo/40");
  expect(stages()).toEqual(["Implement: finished", "Verify: failed", "Review: not reached", "Merge gate: not reached", "Merged: not reached"]);
});

test("a merged run has all five stages finished and no unused round slots", async () => {
  await mount("#/runs/demo/9");
  expect(stages()).toEqual(STAGE_NAMES.map((name) => `${name === "Review" ? "Review · 2 rounds" : name}: finished`));
  expect(screen.queryAllByRole("img", { name: "unused round" })).toEqual([]);
});
