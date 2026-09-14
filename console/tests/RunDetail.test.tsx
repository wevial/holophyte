import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Now } from "../src/components/Now";
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
  expect(screen.getByText("Round 2 of 2 · reviewing")).toBeTruthy();
  expect(document.querySelector("[data-started]")!.textContent).toBe(`started ${formatClock(T)} · writer`);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("10m 00s left in box");
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
    ["Kill run", true],
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
    run: { ...DETAIL.run, phase: "done", ended_ms: T + 82 * MINUTE, outcome: "merged" },
    rounds: [DETAIL.rounds[0]!, { ...DETAIL.rounds[1]!, ended_ms: T + 30 * MINUTE }],
  };
  await mount(finished, T + 82 * MINUTE + 24 * 60 * MINUTE);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("52m over the box");
  expect(box.getAttribute("data-box")).toBe("over");
  cleanup();

  // A live run 40 minutes into the same box reads the card's clock: two
  // seconds on the console's clock grows the figure by two seconds.
  const liveRun: Run = { ...working.runs[0]!, id: 91, started_ms: T, elapsed_ms: 40 * MINUTE };
  const status: Status = { ...working, now: T + 40 * MINUTE, runs: [liveRun] };
  const seen = T + 40 * MINUTE;
  const page = (now: number) => (
    <Now hosts={[hostOf(status, NO_ATTENTION, BASE, seen)]} project="all" now={now} deps={{ fetch: answering(DETAIL) }} />
  );
  const view = render(page(seen));
  fireEvent.click(within(screen.getByRole("listitem")).getByRole("button"));
  await settle();
  expect(document.querySelector("[data-box]")!.textContent).toBe("10m 00s over the box");
  view.rerender(page(seen + 2_000));
  expect(document.querySelector("[data-box]")!.textContent).toBe("10m 02s over the box");
});

test("past the box the header reads 10m 00s over the box in the bad tone and the segments fill the bar", async () => {
  await mount(DETAIL, T + 40 * MINUTE);
  const box = document.querySelector("[data-box]")!;
  expect(box.textContent).toBe("10m 00s over the box");
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
  expect(status().textContent).toBe("fix 1 · 8m 00s");
  expect(document.querySelectorAll("[data-segment-label]").length).toBe(0);
  // Two seconds on the console's clock grows the figure by two seconds.
  view.rerender(page(2_000));
  expect(status().textContent).toBe("fix 1 · 8m 02s");
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
  expect(tooltip()!.textContent).toBe("Implementation · 20m 00s");
  expect((tooltip() as HTMLElement).style.left).toBe("25%");
  fireEvent.mouseOut(items[0]!);
  expect(tooltip()).toBeNull();
  fireEvent.focusIn(items[1]!);
  expect(tooltip()!.textContent).toBe("Review 1 · 2m 00s");
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
  expect(screen.getByText("Round 2 of 2 · reviewing")).toBeTruthy();
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
  expect(buttons[1]!.getAttribute("aria-expanded")).toBe("true");
  const card = within(folds[1]!).getByRole("listitem");
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
