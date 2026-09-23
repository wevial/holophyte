import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import unestimatedDetail from "../../tests/fixtures/serve/run-detail-unestimated.json";
import { TimeBoxBar } from "../src/components/TimeBoxBar";
import { Now } from "../src/components/Now";
import { FindingCard } from "../src/components/FindingCard";
import { RunDetail } from "../src/components/RunDetail";
import { formatClock } from "../src/lib/format";
import type { LedgerRow } from "../src/lib/ledger";
import type { Fetch } from "../src/lib/poll";
import type { Run, RunDetailBody, RunFilesBody, Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, hostOf, settle } from "./harness";

const MINUTE = 60_000;
const T = 1_756_900_000_000;
const BASE = "http://writer:7710";

const working = await fixture<Status>("working.json");

/** `/runs/91` as the daemon serves it: two rounds, the newest open. */
const DETAIL: RunDetailBody = {
  run: {
    id: 91,
    ticket: "KO-232",
    title: "Console: the run detail",
    phase: "reviewing",
    attempt: 1,
    started_ms: T,
    ended_ms: null,
    outcome: null,
    time_box_ms: 30 * MINUTE,
    working_ms: 20 * MINUTE, work_started_ms: T,
    branch: "task/ko-232",
    host: "writer",
    heartbeat_age_ms: 4_000,
    merge_sha: null,
    max_rounds: 2,
  },
  rounds: [
    {
      round: 1,
      started_ms: T + 8 * MINUTE,
      ended_ms: T + 12 * MINUTE,
      verdict: "changes_requested",
      findings: [{ path: "old.py", line: 1, severity: "p1", message: "addressed since" }],
    },
    {
      round: 2,
      started_ms: T + 16 * MINUTE,
      ended_ms: null,
      verdict: "changes_requested",
      findings: [
        { path: "holophyte/serve.py", line: 12, severity: "nit", message: "Trailing comma" },
        {
          path: "/home/reviewer/candidate/holophyte/runs.py",
          line: 40,
          severity: "p1",
          message:
            "- [P1] **Lease is never released** — " +
            "[holophyte/runs.py](/home/reviewer/candidate/holophyte/runs.py:40) " +
            "returns before `release()` runs",
        },
        {
          path: "criteria",
          line: 2,
          severity: "p2",
          message:
            "CRITERION 2: not met — no test exercises the conflict path\n" +
            "Given a merge-gate conflict, when the gate runs, then the run " +
            "goes back to the implementer (tests/test_gates.py witnesses it)",
        },
        {
          path: "holophyte/loop.py",
          line: 345,
          severity: "p0",
          message: "Merge gate conflict fails the run outright",
        },
      ],
    },
  ],
  events: [],
};

/** Files as `/runs/91/files` serves them: the branch has one change so far. */
const FILES: RunFilesBody = {
  files: [{ path: "holophyte/serve.py", status: "M", added: 12, deleted: 3 }],
  total_added: 12,
  total_deleted: 3,
};

const answering =
  (
    body: RunDetailBody,
    files: Response | (() => Response) = () => Response.json(FILES),
    entries: LedgerRow[] = [],
  ): Fetch =>
  async (url) => {
    if (url.endsWith("/runs/91/turns")) return Response.json({ turns: [] });
    if (url.endsWith("/runs/91/files")) return typeof files === "function" ? files() : files;
    if (url.endsWith("/runs/91/ledger")) return Response.json({ run_id: 91, ticket: "KO-232", entries });
    return url.endsWith("/runs/91") ? Response.json(body) : new Response("not found", { status: 404 });
  };

afterEach(cleanup);

async function mount(body: RunDetailBody, now: number, files?: () => Response, entries?: LedgerRow[]) {
  render(<RunDetail base={BASE} id={91} now={now} polls={1} deps={{ fetch: answering(body, files, entries) }} />);
  await settle();
  // A finished run's ledger fetch lands a cycle after the detail does.
  await settle();
}

test("the newest round's findings are cards pilled must, must, should, nit with path:line and a 2 must · 1 should label", async () => {
  await mount(DETAIL, T + 20 * MINUTE);
  const cards = screen.getAllByRole("listitem").filter((item) => item.hasAttribute("data-finding"));
  expect(cards.length).toBe(4);
  expect(cards.map((card) => card.querySelector("[data-severity]")!.textContent)).toEqual([
    "must",
    "must",
    "should",
    "nit",
  ]);
  expect(cards.map((card) => card.querySelector("[data-severity]")!.getAttribute("data-severity"))).toEqual([
    "must",
    "must",
    "should",
    "nit",
  ]);
  expect(cards[0]!.querySelector("[data-severity]")!.className).toContain("bg-bad-bg");
  expect(cards[1]!.querySelector("[data-severity]")!.className).toContain("bg-bad-bg");
  expect(cards[2]!.querySelector("[data-severity]")!.className).toContain("bg-warn-bg");
  // The p1 card's stored path still carries the reviewer container's mount;
  // the location shows the repository's own.
  expect(cards.map((card) => card.querySelector("[data-location]")!.textContent)).toEqual([
    "holophyte/runs.py:40",
    "holophyte/loop.py:345",
    "criteria:2",
    "holophyte/serve.py:12",
  ]);
  expect(within(cards[1]!).getByText("Merge gate conflict fails the run outright")).toBeTruthy();
  expect(document.querySelector("[data-severity-counts]")!.textContent).toBe("2 must · 1 should");
  expect(document.querySelector("[data-files-label]")!.textContent).toBe("1 · +12 −3");
  expect(document.querySelector("[data-file] [data-path]")!.textContent).toBe("holophyte/serve.py");
  expect(screen.getByText("Review 2 of 2 · reviewing")).toBeTruthy();
  expect(document.querySelector("[data-started]")!.textContent).toBe(`started ${formatClock(T)} · writer`);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("10m 00s left in working box · wall 20m 00s");
  expect(box.getAttribute("data-box")).toBe("left");
  const timeline = screen.getByRole("list", { name: "Round timeline" });
  const items = Array.from(timeline.children) as HTMLElement[];
  expect(items.map((item) => item.getAttribute("data-segment"))).toEqual(["implement", "review", "fix", "review", "remaining"]);
  expect(items[3]!.getAttribute("data-running")).toBe("true");
  expect(items[3]!.querySelector(".segment-running")).toBeTruthy();
  expect(items[0]!.querySelector(".segment-running")).toBeNull();
  const actions = Array.from(document.querySelectorAll("footer button")) as HTMLButtonElement[];
  const buttons = actions.map((button) => [button.textContent, button.disabled]);
  expect(buttons).toEqual([
    ["Requeue ticket", true],
  ]);
});

test("a finding card reads as a title, a location and a body; a criterion finding folds its criterion", async () => {
  await mount(DETAIL, T + 20 * MINUTE);
  const cards = screen.getAllByRole("listitem").filter((item) => item.hasAttribute("data-finding"));

  // The [P1] bullet: the bold lead is the title, the location link is out of
  // the body, and the body's inline code renders as code.
  const p1 = cards[0]!;
  expect(p1.querySelector("[data-title]")!.textContent).toBe("Lease is never released");
  const body = p1.querySelector("[data-body]")!;
  expect(body.textContent).toBe("returns before release() runs");
  expect(body.querySelector("code")!.textContent).toBe("release()");
  expect(body.querySelector("a")).toBeNull();

  // The criterion finding: the title names the check and its status, the
  // reason is the body, and the criterion's own text sits inside a closed
  // disclosure that opens on click.
  const criterion = cards[2]!;
  expect(criterion.querySelector("[data-title]")!.textContent).toBe("Criterion 2 · not met");
  expect(criterion.querySelector("[data-body]")!.textContent).toBe("no test exercises the conflict path");
  const toggle = within(criterion).getByRole("button", { name: /criterion/ });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  const folded = criterion.querySelector("[data-criterion]")!;
  expect(folded.hasAttribute("hidden")).toBe(true);
  expect(folded.textContent).toContain("Given a merge-gate conflict");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(folded.hasAttribute("hidden")).toBe(false);
});

test("the header shows no sha for an unmerged run, the plain short sha when merged, and an anchor when commit_url is set", async () => {
  await mount(DETAIL, T + 20 * MINUTE);
  expect(document.querySelector("[data-sha]")).toBeNull();
  cleanup();

  await mount({ ...DETAIL, run: { ...DETAIL.run, merge_sha: "3f9c2ab0c1d2e3f4", commit_url: null } }, T + 20 * MINUTE);
  const plain = document.querySelector("header [data-sha]")!;
  expect(plain.tagName).toBe("SPAN");
  expect(plain.textContent).toBe("3f9c2ab");
  cleanup();

  const url = "https://github.com/example/writer/commit/3f9c2ab0c1d2e3f4";
  await mount({ ...DETAIL, run: { ...DETAIL.run, merge_sha: "3f9c2ab0c1d2e3f4", commit_url: url } }, T + 20 * MINUTE);
  const linked = document.querySelector("header [data-sha]") as HTMLAnchorElement;
  expect(linked.tagName).toBe("A");
  expect(linked.getAttribute("href")).toBe(url);
  expect(linked.getAttribute("target")).toBe("_blank");
  expect(linked.getAttribute("rel")).toBe("noopener noreferrer");
  expect(linked.textContent).toBe("3f9c2ab");
});

test("the header shows a PR #N anchor when pr_url is set and none otherwise", async () => {
  await mount(DETAIL, T + 20 * MINUTE);
  expect(document.querySelector("header [data-pr]")).toBeNull();
  cleanup();

  const url = "https://github.com/o/r/pull/2170";
  await mount({ ...DETAIL, run: { ...DETAIL.run, pr_url: url } }, T + 20 * MINUTE);
  const pr = document.querySelector("header [data-pr]") as HTMLAnchorElement;
  expect(pr.tagName).toBe("A");
  expect(pr.textContent).toBe("PR #2170");
  expect(pr.getAttribute("href")).toBe(url);
  expect(pr.getAttribute("target")).toBe("_blank");
  expect(pr.getAttribute("rel")).toBe("noopener noreferrer");
});

test("a finished run's box figure freezes at its end while a live run's keeps counting on the ticking clock", async () => {
  // Ended 82 minutes after it started against a 30-minute box: the clock a
  // day later still reads the run's own figure.
  const finished: RunDetailBody = {
    ...DETAIL,
    run: { ...DETAIL.run, phase: "done", ended_ms: T + 82 * MINUTE, working_ms: 82 * MINUTE, work_started_ms: null, outcome: "merged" },
    rounds: [DETAIL.rounds[0]!, { ...DETAIL.rounds[1]!, ended_ms: T + 30 * MINUTE }],
  };
  await mount(finished, T + 82 * MINUTE + 24 * 60 * MINUTE);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("52m over the working box · wall 1h 22m");
  expect(box.getAttribute("data-box")).toBe("over");
  cleanup();

  // A live run 40 minutes into the same box reads the card's clock: two
  // seconds on the console's clock grows the figure by two seconds.
  const liveRun: Run = { ...working.runs[0]!, id: 91, started_ms: T, elapsed_ms: 40 * MINUTE };
  const status: Status = { ...working, now: T + 40 * MINUTE, runs: [liveRun] };
  const seen = T + 40 * MINUTE;
  const page = (now: number) => (
    <Now hosts={[hostOf(status, NO_ATTENTION, BASE, seen)]} project="all" now={now} deps={{ fetch: answering({ ...DETAIL, run: { ...DETAIL.run, working_ms: 40 * MINUTE } }) }} />
  );
  const view = render(page(seen));
  fireEvent.click(within(screen.getByRole("listitem")).getByRole("button"));
  await settle();
  expect(document.querySelector("[data-box]")!.textContent).toBe("10m 00s over the working box · wall 40m 00s");
  view.rerender(page(seen + 2_000));
  expect(document.querySelector("[data-box]")!.textContent).toBe("10m 02s over the working box · wall 40m 02s");
});

test("the box figure reads the agent clock, not verify, when the daemon serves it", async () => {
  await mount({ ...DETAIL, run: { ...DETAIL.run, time_box_ms: 20 * MINUTE, working_ms: 25 * MINUTE, agent_ms: 12 * MINUTE, verify_ms: 13 * MINUTE, verify_started_ms: null } }, T + 25 * MINUTE);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("8m 00s left in working box · wall 25m 00s");
  expect(box.getAttribute("data-box")).toBe("left");
});

test("beside the box the header splits the run's time into agent 10m 00s · verify 12m 00s", async () => {
  await mount({ ...DETAIL, run: { ...DETAIL.run, working_ms: 22 * MINUTE, work_started_ms: null, agent_ms: 10 * MINUTE, verify_ms: 12 * MINUTE, verify_started_ms: null } }, T + 22 * MINUTE);
  expect(document.querySelector("[data-clocks]")!.textContent).toBe("agent 10m 00s · verify 12m 00s");
});

test("between polls only the open span's figure counts on: verify while verify runs, agent while a turn does", async () => {
  const seen = T + 22 * MINUTE;
  const split = { working_ms: 22 * MINUTE, work_started_ms: T, agent_ms: 10 * MINUTE, verify_ms: 12 * MINUTE };
  const page = (run: Partial<RunDetailBody["run"]>, sinceMs: number) => (
    <RunDetail base={BASE} id={91} now={seen} sinceMs={sinceMs} polls={1}
      deps={{ fetch: answering({ ...DETAIL, run: { ...DETAIL.run, ...split, ...run } }) }} />
  );
  const clocks = () => document.querySelector("[data-clocks]")!.textContent;
  const verifying = render(page({ verify_started_ms: T + 20 * MINUTE }, 0));
  await settle();
  expect(clocks()).toBe("agent 10m 00s · verify 12m 00s");
  verifying.rerender(page({ verify_started_ms: T + 20 * MINUTE }, 2_000));
  expect(clocks()).toBe("agent 10m 00s · verify 12m 02s");
  cleanup();

  const turning = render(page({ verify_started_ms: null }, 0));
  await settle();
  expect(clocks()).toBe("agent 10m 00s · verify 12m 00s");
  turning.rerender(page({ verify_started_ms: null }, 2_000));
  expect(clocks()).toBe("agent 10m 02s · verify 12m 00s");
});

test("a run recorded before the split reads verify n/a beside its agent figure", async () => {
  await mount({ ...DETAIL, run: { ...DETAIL.run, working_ms: 20 * MINUTE, work_started_ms: null, verify_ms: null } }, T + 20 * MINUTE);
  expect(document.querySelector("[data-clocks]")!.textContent).toBe("agent 20m 00s · verify n/a");
});

test("past the box the header reads 10m 00s over the box in the bad tone and the segments fill the bar", async () => {
  await mount({ ...DETAIL, run: { ...DETAIL.run, working_ms: 40 * MINUTE } }, T + 40 * MINUTE);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("10m 00s over the working box · wall 40m 00s");
  expect(box.getAttribute("data-box")).toBe("over");
  expect(box.className).toContain("text-bad");
  const timeline = screen.getByRole("list", { name: "Round timeline" });
  const items = Array.from(timeline.children) as HTMLElement[];
  expect(items.map((item) => item.getAttribute("data-segment"))).toEqual(["implement", "review", "fix", "review"]);
  // Each item is `calc(P% - Qpx)`: the percentages sum to 100 and the px
  // subtractions sum to the three 3px gaps, so items plus gaps fit the bar.
  const parts = items.map((item) => {
    const match = /^calc\((\S+)% - (\S+)px\)$/.exec(item.style.width);
    expect(match).toBeTruthy();
    return { percent: parseFloat(match![1]!), px: parseFloat(match![2]!) };
  });
  expect(parts.reduce((sum, part) => sum + part.percent, 0)).toBeCloseTo(100, 6);
  expect(parts.reduce((sum, part) => sum + part.px, 0)).toBeCloseTo(3 * (items.length - 1), 6);
  expect(timeline.style.gap).toBe("3px");
});

/** A finished run whose phase changes leave the bar three segments of
 *  shares 0.5, 0.05 and 0.45: implement 20m, review 2m, verify 18m of a
 *  40m run past its 30m box. */
const SHARES: RunDetailBody = {
  ...DETAIL,
  run: { ...DETAIL.run, phase: "done", ended_ms: T + 40 * MINUTE, outcome: "merged" },
  rounds: [],
  events: [
    { at: T, kind: "phase_change", summary: "claimed -> working: KO-232" },
    { at: T + 20 * MINUTE, kind: "phase_change", summary: "working -> reviewing: round 1 review" },
    { at: T + 22 * MINUTE, kind: "phase_change", summary: "reviewing -> verifying: approved" },
    { at: T + 40 * MINUTE, kind: "phase_change", summary: "verifying -> done: merged" },
  ],
};

test("a finished run's bar carries no labels and its status line reads done with the run's whole span", async () => {
  await mount(SHARES, T + 40 * MINUTE);
  const bar = screen.getByRole("list", { name: "Round timeline" });
  expect(Array.from(bar.children).map((item) => item.getAttribute("data-segment"))).toEqual([
    "implement",
    "review",
    "verify",
  ]);
  expect(document.querySelectorAll("[data-segment-label]").length).toBe(0);
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("done · 40m 00s");
});

/** A live run two minutes into a fix round: claimed -> working, round 1's
 *  review at T+8m, the fix open from T+12m. */
const FIXING: RunDetailBody = {
  ...DETAIL,
  run: { ...DETAIL.run, phase: "addressing" },
  rounds: [],
  events: [
    { at: T, kind: "phase_change", summary: "claimed -> working: KO-232" },
    { at: T + 8 * MINUTE, kind: "phase_change", summary: "working -> reviewing: round 1 review" },
    { at: T + 12 * MINUTE, kind: "phase_change", summary: "reviewing -> addressing: round 1: 2 findings to address" },
  ],
};

test("a live run's status line names the running fix phase and its duration ticks with the clock", async () => {
  const seen = T + 20 * MINUTE;
  const page = (sinceMs: number) => (
    <RunDetail base={BASE} id={91} now={seen} sinceMs={sinceMs} polls={1} deps={{ fetch: answering(FIXING) }} />
  );
  const view = render(page(0));
  await settle();
  const status = () => document.querySelector("[data-timeline-status]")!;
  // The open fix began at T+12m: eight minutes in at the poll.
  expect(status().textContent).toBe("fix · 8m 00s");
  expect(document.querySelectorAll("[data-segment-label]").length).toBe(0);
  // Two seconds on the console's clock grows the figure by two seconds.
  view.rerender(page(2_000));
  expect(status().textContent).toBe("fix · 8m 02s");
});

test("a run done at 82m 14s reads done with the run's total span", async () => {
  const done: RunDetailBody = {
    ...DETAIL,
    run: { ...DETAIL.run, phase: "done", ended_ms: T + 82 * MINUTE + 14_000, outcome: "merged" },
    rounds: [],
    events: [
      { at: T, kind: "phase_change", summary: "claimed -> working: KO-232" },
      { at: T + 60 * MINUTE, kind: "phase_change", summary: "working -> verifying: round 1: verify before review" },
      { at: T + 80 * MINUTE, kind: "phase_change", summary: "verifying -> merging: approved" },
      { at: T + 82 * MINUTE + 14_000, kind: "phase_change", summary: "merging -> done: merged" },
    ],
  };
  await mount(done, T + 82 * MINUTE + 14_000);
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("done · 82m 14s");
});

test("a finished run's done figure is its whole span, not the stretch the segments cover", async () => {
  // Claimed at T but working only from T+1m: the segments cover nine of
  // the run's ten minutes.
  const done: RunDetailBody = {
    ...DETAIL,
    run: { ...DETAIL.run, phase: "done", ended_ms: T + 10 * MINUTE, outcome: "merged" },
    rounds: [],
    events: [
      { at: T + MINUTE, kind: "phase_change", summary: "claimed -> working: KO-232" },
      { at: T + 9 * MINUTE, kind: "phase_change", summary: "working -> verifying: approved" },
      { at: T + 10 * MINUTE, kind: "phase_change", summary: "verifying -> done: merged" },
    ],
  };
  await mount(done, T + 10 * MINUTE);
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("done · 10m 00s");
});

test("a run that ended before any segment opened still reads done with the run's span", async () => {
  // claimed -> failed maps to no segment kind, so the bar is an empty
  // track: the done line comes from the run, not the segments.
  const failed: RunDetailBody = {
    ...DETAIL,
    run: { ...DETAIL.run, phase: "failed", ended_ms: T + MINUTE, outcome: "failed" },
    rounds: [],
    events: [{ at: T, kind: "phase_change", summary: "claimed -> failed: lease lost" }],
  };
  await mount(failed, T + MINUTE);
  const bar = screen.getByRole("list", { name: "Round timeline" });
  expect(Array.from(bar.children).map((item) => item.getAttribute("data-segment"))).toEqual(["remaining"]);
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("done · 1m 00s");
});

test("a run parked on merge approval is live, not done: the status line names the waiting phase and the wait ticks", async () => {
  const parked: RunDetailBody = {
    ...DETAIL,
    run: { ...DETAIL.run, phase: "awaiting_merge_approval" },
    rounds: [],
    events: [
      { at: T, kind: "phase_change", summary: "claimed -> working: KO-232" },
      { at: T + 30 * MINUTE, kind: "phase_change", summary: "working -> merging: approved" },
      { at: T + 32 * MINUTE, kind: "phase_change", summary: "merging -> awaiting_merge_approval: candidate parked" },
    ],
  };
  const seen = T + 37 * MINUTE;
  const page = (sinceMs: number) => (
    <RunDetail base={BASE} id={91} now={seen} sinceMs={sinceMs} polls={1} deps={{ fetch: answering(parked) }} />
  );
  const view = render(page(0));
  await settle();
  const status = () => document.querySelector("[data-timeline-status]")!;
  // The merge segment closed at T+32m but the run has no ended_ms: the
  // line names the parking phase and how long it has waited, and keeps
  // counting.
  expect(status().textContent).toBe("awaiting_merge_approval · 5m 00s");
  view.rerender(page(2_000));
  expect(status().textContent).toBe("awaiting_merge_approval · 5m 02s");
});

test("a segment floats its long name and duration on hover and on focus, hides on leave and blur, and carries no title", async () => {
  await mount(SHARES, T + 40 * MINUTE);
  const bar = screen.getByRole("list", { name: "Round timeline" });
  const items = Array.from(bar.querySelectorAll("li")) as HTMLElement[];
  expect(items.every((item) => item.getAttribute("title") == null)).toBe(true);
  expect(items.every((item) => item.getAttribute("tabindex") === "0")).toBe(true);
  const tooltip = () => document.querySelector("[data-segment-tooltip]");
  expect(tooltip()).toBeNull();
  fireEvent.mouseOver(items[0]!);
  expect(tooltip()!.textContent).toBe("Implementation · 20m 00s · claimed -> working: KO-232");
  expect((tooltip() as HTMLElement).style.left).toBe("25%");
  fireEvent.mouseOut(items[0]!);
  expect(tooltip()).toBeNull();
  fireEvent.focusIn(items[1]!);
  expect(tooltip()!.textContent).toBe("Review · 2m 00s · working -> reviewing: round 1 review");
  fireEvent.focusOut(items[1]!);
  expect(tooltip()).toBeNull();
});

test("a newest round that passed shows no open findings and zero counts", async () => {
  const passed: RunDetailBody = {
    ...DETAIL,
    rounds: [DETAIL.rounds[0]!, { ...DETAIL.rounds[1]!, ended_ms: T + 19 * MINUTE, verdict: "pass", findings: [] }],
  };
  await mount(passed, T + 20 * MINUTE);
  expect(screen.getByText("No open findings")).toBeTruthy();
  expect(document.querySelector("[data-severity-counts]")!.textContent).toBe("0 must · 0 should");
  expect(document.querySelector("[data-finding]")).toBeNull();
});

test("a files endpoint answering 409 leaves one line, its own message, and the rest of the card renders", async () => {
  const gone = () => Response.json({ error: "branch task/ko-232 is not on disk", run: 91 }, { status: 409 });
  await mount({ ...DETAIL, events: [{ at: T, kind: "claimed", summary: "claimed KO-232" }] }, T + 20 * MINUTE, gone);
  const column = screen.getByRole("region", { name: "Files touched" });
  expect(column.querySelector("[data-files-note]")!.textContent).toBe("branch task/ko-232 is not on disk");
  expect(column.querySelector("[data-file]")).toBeNull();
  expect(column.querySelector("[data-files-label]")).toBeNull();
  expect(screen.getByText("Review 2 of 2 · reviewing")).toBeTruthy();
  expect(document.querySelectorAll("[data-finding]").length).toBe(4);
  expect(document.querySelector("[data-log-summary]")!.textContent).toBe("1 event · last: claimed KO-232 20m ago");
  expect(document.querySelector("[data-log-rows]")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Run log/ }));
  expect(document.querySelectorAll("[data-log-row]").length).toBe(1);
  expect(document.querySelector("[data-detail-error]")).toBeNull();
});

/** DETAIL ended failed: round 2's findings were still standing when the
 *  run stopped. */
const FAILED: RunDetailBody = {
  ...DETAIL,
  run: { ...DETAIL.run, phase: "failed", ended_ms: T + 25 * MINUTE, outcome: "failed" },
  rounds: [DETAIL.rounds[0]!, { ...DETAIL.rounds[1]!, ended_ms: T + 19 * MINUTE }],
};

test("a run that ended failed shows a Findings section whose last-round findings carry the open chip", async () => {
  await mount(FAILED, T + 25 * MINUTE);
  expect(screen.getByText("Findings")).toBeTruthy();
  expect(screen.queryByText("Open findings")).toBeNull();
  expect(document.querySelector("[data-findings-count]")!.textContent).toBe("5 over 2 rounds");
  // The newest fold is open: round 2's four findings, each chipped open.
  const open = Array.from(document.querySelectorAll('[data-fate="open"]'));
  expect(open.length).toBe(4);
  expect(document.querySelectorAll("[data-finding]").length).toBe(4);
});

test("only the newest round's fold is open and an older round's header opens it", async () => {
  const entries: LedgerRow[] = [
    {
      at: T + 13 * MINUTE,
      run: 91,
      ticket: "KO-232",
      kind: "round",
      source: "loop",
      text:
        "Round 1: REQUEST_CHANGES -> fix round\n" +
        "Reviewer findings:\n- [P1] old.py:1 needs a guard\n\n" +
        "Implementer response:\nDECLINE old.py — superseded by the rewrite",
    },
  ];
  await mount(FAILED, T + 25 * MINUTE, undefined, entries);
  // Round 1 reads "fixed" until its ledger row lands, which can be a tick
  // after mount() returns; wait for the header the ledger decides.
  await screen.findByRole("button", { name: /Round 1 · 1 finding · declined/ });
  const folds = Array.from(document.querySelectorAll("[data-round-fold]")) as HTMLElement[];
  // Newest first on the page: round 2, then round 1.
  expect(folds.map((fold) => fold.getAttribute("data-round-fold"))).toEqual(["2", "1"]);
  // The header button is the fold's own; an open card's criterion fold is
  // a button too, nested deeper.
  const buttons = folds.map((fold) => fold.querySelector(":scope > button") as HTMLButtonElement);
  expect(buttons[0]!.getAttribute("aria-expanded")).toBe("true");
  expect(buttons[0]!.textContent).toContain("Round 2 · 4 findings · open");
  expect(buttons[1]!.getAttribute("aria-expanded")).toBe("false");
  expect(buttons[1]!.textContent).toContain("Round 1 · 1 finding · declined");
  // The closed fold holds no cards; opening it shows round 1's finding
  // declined, with the implementer's line under the body.
  expect(within(folds[1]!).queryAllByRole("listitem").length).toBe(0);
  fireEvent.click(buttons[1]!);
  const card = await within(folds[1]!).findByRole("listitem");
  expect(buttons[1]!.getAttribute("aria-expanded")).toBe("true");
  expect(card.querySelector("[data-fate]")!.textContent).toBe("declined");
  expect(card.querySelector("[data-fate-sentence]")!.textContent).toBe(
    "DECLINE old.py — superseded by the rewrite",
  );
});

test("a merged run's findings history lists every round, the approving one empty", async () => {
  const merged: RunDetailBody = {
    ...FAILED,
    run: { ...FAILED.run, phase: "done", outcome: "merged" },
    rounds: [
      ...FAILED.rounds,
      { round: 3, started_ms: T + 20 * MINUTE, ended_ms: T + 24 * MINUTE, verdict: "pass", findings: [] },
    ],
  };
  await mount(merged, T + 25 * MINUTE);
  expect(document.querySelector("[data-findings-count]")!.textContent).toBe("5 over 3 rounds");
  const folds = Array.from(document.querySelectorAll("[data-round-fold]")) as HTMLElement[];
  expect(folds.map((fold) => fold.getAttribute("data-round-fold"))).toEqual(["3", "2", "1"]);
  const newest = within(folds[0]!).getByRole("button");
  expect(newest.getAttribute("aria-expanded")).toBe("true");
  expect(newest.textContent).toContain("Round 3 · 0 findings");
});

test("a 404 says the run is not in the store and the Floor row still collapses and re-expands", async () => {
  const run: Run = { ...working.runs[0]!, id: 91, started_ms: T };
  const status: Status = { ...working, now: T + 20 * MINUTE, runs: [run] };
  const missing: Fetch = async () => Response.json({ error: "no such run", run: 91 }, { status: 404 });
  render(<Now hosts={[hostOf(status, NO_ATTENTION, BASE)]} project="all" now={status.now} deps={{ fetch: missing }} />);
  const row = screen.getByRole("listitem");
  const toggle = within(row).getByRole("button");
  fireEvent.click(toggle);
  await settle();
  expect(row.querySelector("[data-detail-error]")!.textContent).toBe("run 91 is not in the store");
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(row.querySelector("[data-detail]")).toBeNull();
  fireEvent.click(toggle);
  await settle();
  expect(row.querySelector("[data-detail-error]")!.textContent).toBe("run 91 is not in the store");
});

test("bot findings stay visible as advisory on active and completed run cards", async () => {
  for (const ended_ms of [null, T + 20 * MINUTE]) {
    await mount({
      ...DETAIL,
      run: { ...DETAIL.run, ended_ms },
      findings: [{ tone: "advisory", message: "https://example.test/thread: Consider a rename" }],
    }, T + 20 * MINUTE);
    const findings = screen.getByRole("list", { name: "Advisory findings" });
    expect(within(findings).getByText("advisory")).toBeTruthy();
    expect(findings.textContent).toContain("https://example.test/thread: Consider a rename");
    cleanup();
  }
});


test("instructions show each request once with its state and thread link", async () => {
  const body = structuredClone(DETAIL);
  body.rounds[1]!.instructions = [
    { kind: "instruction", path: "app.py", line: 30, author: "operator", request: "Use the path token", url: "https://example.com/thread/1", outcome: "changed", reply: "Addressed in abc: used path token", triage: { decision: "fix", confidence: 0.9, route: "fix", reason: "fix" } },
    { kind: "instruction", path: "app.py", line: 40, author: "maintainer", request: "Preserve validation", url: "https://example.com/thread/2" },
  ];
  await mount(body, T + 20 * MINUTE);
  expect(screen.getAllByText("Use the path token")).toHaveLength(1);
  expect(screen.getAllByText("Preserve validation")).toHaveLength(1);
  expect(screen.getByText("changed")).toBeTruthy();
  expect(screen.getByText("awaiting fix")).toBeTruthy();
  expect(screen.getByRole("link", { name: "app.py:30" }).getAttribute("href")).toBe("https://example.com/thread/1");
  expect(screen.getByText("@maintainer")).toBeTruthy();
  expect(screen.getByText(/Triage: fix · confidence 0.9/)).toBeTruthy();
  expect(screen.queryByText(/MENTIONED|VERDICT/)).toBeNull();
});

test("send-back notes are markdown cards with metadata outside the body", async () => {
  const body = structuredClone(DETAIL);
  body.rounds[1]!.operator_notes = [{ kind: "operator_note", event_id: 4047, author: "maintainer",
    note: "1. Fix validation\n2. Preserve the path\n3. Verify the result\n\nKeep this change focused." }];
  body.events = [{ at: T + 15 * MINUTE, kind: "operator_note_consumed", summary: "operator_note event 4047 drove round 2" }];
  await mount(body, T + 20 * MINUTE);
  const cards = document.querySelectorAll("[data-operator-note]");
  expect(cards).toHaveLength(1);
  const card = cards[0]!;
  expect(card.querySelector("header")!.textContent).toContain("maintainer");
  expect(card.querySelector("header")!.textContent).toContain(formatClock(T + 15 * MINUTE));
  expect(card.querySelector("header")!.textContent).toContain("Round 2");
  expect(card.querySelector("header")!.textContent).toContain("4047");
  const content = card.querySelector("[data-note-body]")!;
  expect(Array.from(content.querySelectorAll("ol li"), li => li.textContent)).toEqual([
    "Fix validation", "Preserve the path", "Verify the result",
  ]);
  expect(content.querySelector("p")!.textContent).toBe("Keep this change focused.");
  expect(content.textContent).not.toContain("operator_note event");
});

test("a plain send-back sentence stays unchanged", async () => {
  const body = structuredClone(DETAIL);
  body.rounds[0]!.operator_notes = [{ kind: "operator_note", event_id: 4048, author: "maintainer", note: "Please preserve validation." }];
  await mount(body, T + 20 * MINUTE);
  expect(document.querySelector("[data-note-body] p")?.textContent).toBe("Please preserve validation.");
});

test("recorded rounds determine the header, current chip and findings labels", async () => {
  const body = structuredClone(DETAIL);
  body.run.max_rounds = 4;
  body.rounds = [
    { ...DETAIL.rounds[0]!, round: 6 },
    { ...DETAIL.rounds[1]!, round: 9, ended_ms: T + 18 * MINUTE },
    { ...DETAIL.rounds[1]!, round: 12, started_ms: T + 19 * MINUTE },
  ];
  // An incomplete event stream and ticket-wide round IDs must not change the count.
  body.events = [{ at: T + 19 * MINUTE, kind: "phase_change", summary: "verifying -> reviewing: round 12 review" }];
  for (const events of [body.events, []]) {
    await mount({ ...body, events }, T + 20 * MINUTE);
    expect(screen.getByText("Review 3 of 4 · reviewing")).toBeTruthy();
    expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("Review · Round 3 · 1m 00s");
    expect(document.querySelector("[data-round-fold] button")!.textContent).toContain("Round 3 · 4 findings");
    expect(document.querySelector("[data-round-fold] button")!.textContent).not.toContain("of 4");
    cleanup();
  }
  await mount({ ...body, run: { ...body.run, ended_ms: T + 20 * MINUTE, phase: "done" } }, T + 20 * MINUTE);
  expect(document.querySelector("[data-round-fold] button")!.textContent).toContain("Round 3 · 4 findings");
  expect(document.querySelector("[data-round-fold] button")!.textContent).not.toContain("of 4");
});


test("before any recorded review the header keeps its preview and no chip or fold names a round", async () => {
  await mount({ ...DETAIL, run: { ...DETAIL.run, phase: "working", max_rounds: 4 }, rounds: [], events: [] }, T + MINUTE);
  expect(screen.getByText("Review 0 of 4 · implementing")).toBeTruthy();
  expect(screen.getByText("No review round yet")).toBeTruthy();
  expect(document.querySelector("[data-timeline-status]")!.textContent).toBe("implementing · 1m 00s");
  expect(document.querySelector("[data-round-fold]")).toBeNull();
});


test("thread findings lead with summary and verdict and disclose the original on demand", async () => {
  await mount({ ...DETAIL, rounds: [{ ...DETAIL.rounds[1]!, findings: [{
    kind: "thread", path: "app.py", line: 7, severity: "nit", author: "review-bot",
    author_kind: "bot", verdict: "ADDRESS", summary: "the index keeps a forced file",
    message: "the index keeps a forced file", raw: "<details>Original analysis. </details>".repeat(200),
    url: "https://example.test/thread",
  }] }] }, T + 20 * MINUTE);
  const card = document.querySelector("[data-finding]")!;
  expect(card.textContent).toContain("the index keeps a forced file");
  expect(card.querySelector("[data-verdict]")!.textContent).toBe("ADDRESS");
  expect(card.querySelector("[data-author]")!.textContent).toContain("review-bot");
  expect(card.querySelector("[data-location]")!.textContent).toBe("app.py:7");
  expect(card.textContent).not.toContain("Original analysis.");
  fireEvent.click(screen.getByRole("button", { name: "Show original comment" }));
  expect(card.textContent).toContain("Original analysis.");
  fireEvent.click(screen.getByRole("button", { name: "Hide original comment" }));
  expect(card.textContent).not.toContain("Original analysis.");
});

test("a contract failure replaces the last good cards with one endpoint and field banner", async () => {
  let body: unknown = DETAIL;
  const deps = { fetch: (async (url: string) => Response.json(url.endsWith('/files') ? FILES : body)) as Fetch };
  const view = render(<RunDetail base={BASE} id={91} now={T} polls={0} deps={deps} />);
  await act(settle);
  expect(screen.queryByRole('article', { name: 'run 91' })).not.toBeNull();
  body = { ...DETAIL, run: { ...DETAIL.run, phase: 42 } };
  view.rerender(<RunDetail base={BASE} id={91} now={T} polls={1} deps={deps} />);
  await act(settle);
  expect(screen.getAllByRole('alert')).toHaveLength(1);
  expect(screen.getByRole('alert').textContent).toContain(`${BASE}/runs/91`);
  expect(screen.getByRole('alert').textContent).toContain('run.phase');
  expect(screen.queryByRole('article', { name: 'run 91' })).toBeNull();
  expect(document.querySelector('[data-finding]')).toBeNull();
});

test("the floor names a status contract failure and hides that daemon's old rows", async () => {
  const { pollPeers } = await import("../src/hooks/usePeers");
  const { mergeHosts } = await import("../src/lib/hosts");
  const previous = [hostOf(working, NO_ATTENTION)];
  const results = await pollPeers(BASE, previous, { fetch: async (url) =>
    Response.json(url.endsWith("/status") ? { ...working, runs: "changed" } : NO_ATTENTION),
  });
  render(<Now hosts={mergeHosts(previous, results, T)} project="all" now={T}
    deps={{ fetch: async () => new Response("", { status: 404 }) }} />);
  await act(settle);
  const floor = screen.getByRole("region", { name: "Floor" });
  expect(within(floor).getByRole("alert").textContent).toContain(`${BASE}/status at runs`);
  expect(floor.querySelector("[data-run]")).toBeNull();
  expect(within(floor).queryByText("Nothing on the floor")).toBeNull();
});

test("an unestimated legacy run renders with an unknown budget, not a contract error or overrun", async () => {
  await mount(unestimatedDetail, unestimatedDetail.run.started_ms + 20 * MINUTE);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByRole("article", { name: "run 1" })).toBeTruthy();
  expect(screen.getByText(/working box unknown/)).toBeTruthy();
  expect(document.querySelector('[data-box="over"]')).toBeNull();
  render(<TimeBoxBar elapsedMs={20 * MINUTE} boxMs={null} />);
  const bar = screen.getByRole("progressbar", { name: "Working time budget unknown" });
  expect(bar.hasAttribute("aria-valuenow")).toBe(false);
  expect(bar.getAttribute("data-tone")).toBe("none");
  expect(screen.getByText("working 20m 0s / n/a")).toBeTruthy();
});

test("header counts independent reviews separately from mechanical and bot rounds", async () => {
  await mount({ ...DETAIL, rounds: ["independent-review", "mechanical:main-refresh",
    "mechanical:main-refresh", "github:review-bot"].map((reviewer_model, i) => ({
      ...DETAIL.rounds[0]!, round: i + 1, reviewer_model,
    })) }, T + 20 * MINUTE);
  expect(screen.getByText("Review 1 of 2 · 3 other rounds · reviewing")).toBeTruthy();
});

test("header excludes failed independent reviews from the review budget", async () => {
  await mount({ ...DETAIL, rounds: [
    { ...DETAIL.rounds[0]!, round: 1, reviewer_model: "independent-review", verdict: "error" },
    { ...DETAIL.rounds[0]!, round: 2, reviewer_model: "independent-review", verdict: "approve" },
    { ...DETAIL.rounds[0]!, round: 3, reviewer_model: "independent-review", verdict: "error" },
  ] }, T + 20 * MINUTE);
  expect(screen.getByText("Review 1 of 2 · 2 other rounds · reviewing")).toBeTruthy();
});

test("finding summaries identify declined reasons and leave other verdicts unchanged", () => {
  for (const verdict of ["DECLINE", "ADDRESS", "FOLLOW_UP"]) {
    const { container, unmount } = render(<FindingCard finding={{ message: "original",
      path: "example.py", line: 1, severity: "p2", summary: "Verification now passes", verdict }} />);
    expect(container.querySelector("[data-body]")!.textContent).toBe(
      verdict === "DECLINE" ? "Declined: Verification now passes" : "Verification now passes");
    unmount();
  }
});

test("run page renders babysitter waits and a twelve minute fix as labelled segments", async () => {
  await mount({ ...DETAIL, run: { ...DETAIL.run, phase: "merge_gate", pr_url: "https://example/pr/1" },
    rounds: [], events: [
      { at: T, kind: "phase_change", summary: "reviewing -> merge_gate: babysitting" },
      { at: T, kind: "babysit_step", summary: "checks" },
      { at: T + 2 * MINUTE, kind: "babysit_step", summary: "fix" },
      { at: T + 14 * MINUTE, kind: "babysit_step", summary: "quiet" },
    ] }, T + 16 * MINUTE);
  const timeline = screen.getByRole("list", { name: "Round timeline" });
  expect(within(timeline).getByRole("img", { name: "checks 2m 00s" })).toBeTruthy();
  expect(within(timeline).getByRole("img", { name: "fix 12m 00s" })).toBeTruthy();
  expect(within(timeline).getByRole("img", { name: "quiet 2m 00s" })).toBeTruthy();
});

test("a pending files 409 renders the branch wait in muted text", async () => {
  const pending = () => Response.json({ error: "branch task/ko-232 not cut yet", run: 91, pending: true }, { status: 409 });
  await mount(DETAIL, T + 20 * MINUTE, pending);
  const note = screen.getByRole("region", { name: "Files touched" }).querySelector("[data-files-note]")!;
  expect(note.textContent).toBe("branch task/ko-232 not cut yet");
  expect(note.className).toContain("text-muted");
  expect(note.className).not.toContain("text-bad");
});

test("Turns lists recorded sessions and opens rendered transcript entries in a panel", async () => {
  const requested: string[] = [];
  const fetch: Fetch = async url => {
    requested.push(url);
    if (url.endsWith("/turns")) return Response.json({ turns: [
      { id: 4, role: "implement", label: "claude-implement opus", route: "primary", seconds: 12, session_id: "session-one" },
      { id: 5, role: "adjudicate", label: null, route: "primary", seconds: 3, session_id: null },
    ] });
    if (url.endsWith("/turns/4/transcript")) return Response.json({ entries: [
      { speaker: "user", text: "Check the project." },
      { speaker: "command", text: "echo checked" },
      { speaker: "tool", text: "checked\nExit code: 0" },
      { speaker: "assistant", text: "All checks passed. <script>literal</script>" },
    ] });
    return answering(DETAIL)(url);
  };
  render(<RunDetail base={BASE} id={91} now={T} polls={1} deps={{ fetch }} />);
  await screen.findByRole("link", { name: "Open transcript" });
  const turns = screen.getByRole("region", { name: "Turns" });
  const rows = within(turns).getAllByRole("listitem").map(row => row.textContent);
  expect(rows[0]).toContain("implement · claude-implement opus · primary · 12.0 s · session-one");
  expect(rows[1]).toContain("adjudicate · label unknown · primary · 3.0 s");
  expect(requested.some(url => url.endsWith("/transcript"))).toBe(false);
  fireEvent.click(within(turns).getByRole("link", { name: "Open transcript" }));
  const panel = screen.getByRole("region", { name: "Transcript" });
  await within(panel).findByText("Check the project.");
  expect(panel.textContent).toContain("Exit code: 0");
  expect(panel.textContent).toContain("All checks passed. <script>literal</script>");
  expect(panel.querySelector("script")).toBeNull();
  fireEvent.click(within(panel).getByRole("button", { name: "Close transcript" }));
  expect(screen.queryByRole("region", { name: "Transcript" })).toBeNull();
});

test("Turns from an older daemon without labels still render their rows", async () => {
  const fetch: Fetch = async url => url.endsWith("/turns")
    ? Response.json({ turns: [{ id: 6, role: "implement", route: "fallback", seconds: 7, session_id: null }] })
    : answering(DETAIL)(url);
  render(<RunDetail base={BASE} id={91} now={T} polls={1} deps={{ fetch }} />);
  const turns = await screen.findByRole("region", { name: "Turns" });
  await within(turns).findByText("implement · label unknown · fallback · 7.0 s · no session recorded");
  expect(within(turns).queryByRole("alert")).toBeNull();
});

test("an unavailable transcript explains the missing file or opt-in inside the panel", async () => {
  const fetch: Fetch = async url => url.endsWith("/turns")
    ? Response.json({ turns: [{ id: 8, role: "review", route: "primary", seconds: 4, session_id: "review-session" }] })
    : answering(DETAIL)(url);
  render(<RunDetail base={BASE} id={91} now={T} polls={1} deps={{ fetch }} />);
  await screen.findByRole("link", { name: "Open transcript" });
  fireEvent.click(screen.getByRole("link", { name: "Open transcript" }));
  const alert = await within(screen.getByRole("region", { name: "Transcript" })).findByRole("alert");
  expect(alert.textContent).toContain("Transcript unavailable");
});

test("the header shows the implementer and reviewer the run's turns used as chips, and no reviewer for a run never reviewed", async () => {
  const header = async (turns: object[]) => {
    cleanup();
    const fetch: Fetch = async url => url.endsWith("/turns") ? Response.json({ turns }) : answering(DETAIL)(url);
    render(<RunDetail base={BASE} id={91} now={T} polls={1} deps={{ fetch }} />);
    const card = await screen.findByRole("article", { name: "run 91" });
    await within(screen.getByRole("region", { name: "Turns" })).findAllByRole("listitem");
    return card.querySelector("header")!;
  };
  const chips = (header: Element) => [...header.querySelectorAll("[data-seat]")].map((chip) => chip.textContent);
  const implement = { id: 1, role: "implement", label: "claude-implement opus", route: "primary", seconds: 40, session_id: null };
  const review = { id: 2, role: "review", label: "codex-review gpt-6-astra", route: "primary", seconds: 9, session_id: null };
  expect(chips(await header([implement, review]))).toEqual(["Implementer Claude · Opus", "Reviewer Codex · GPT-6 Astra"]);
  const unreviewed = await header([implement]);
  expect(chips(unreviewed)).toEqual(["Implementer Claude · Opus"]);
  expect(unreviewed.textContent).not.toContain("Reviewer");
});
