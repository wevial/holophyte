import { afterEach, expect, setSystemTime, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NeedsYou } from "../src/components/NeedsYou";
import { ACTIONS_OFF, NOT_WIRED, ROUTES } from "../src/lib/actions";
import type { Ledgers } from "../src/hooks/useLedger";
import type { Attention, AttentionItem, Status } from "../src/lib/types";
import { captureIntervals, fixture, hostOf } from "./harness";

const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");

afterEach(cleanup);

const rows = () => screen.getAllByRole("listitem");
const pills = () => rows().map((row) => row.querySelector("[data-kind]")?.textContent);
const chips = () =>
  within(screen.getByRole("group", { name: "Kinds" }))
    .getAllByRole("button")
    .map((chip) => chip.textContent);

test("the fixture renders four things, one chip per kind with counts, rows longest-waited first and no oldest note", () => {
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
  expect(screen.getByText("4").hasAttribute("data-count")).toBe(true);
  expect(screen.getByText("things need you")).toBeTruthy();
  expect(screen.queryByText(/^oldest/)).toBeNull();
  expect(chips()).toEqual(["All 4", "Questions 1", "Stale runs 1", "Failed 1", "Supervisor 1"]);
  expect(pills()).toEqual(["failed", "supervisor", "stale run", "question"]);
  expect(rows().map((row) => row.getAttribute("data-kind"))).toEqual(["failed", "supervisor", "stale_run", "blocked"]);
  expect(screen.getByText("No heartbeat for 7m 01s while reviewing")).toBeTruthy();
  expect(screen.getByText("Supervisor heartbeat is 20m 00s old (threshold 3m)")).toBeTruthy();
  expect(screen.getAllByText("writer").length).toBe(4);
});

test("rows from two hosts read longest-waited first, equal ages keep daemon order, an ageless item last", () => {
  const now = allKinds.status.now;
  const question = (ticket: string, minutesAgo: number | null): AttentionItem => ({
    kind: "blocked",
    level: "attention",
    ticket,
    question: `${ticket}?`,
    ...(minutesAgo == null ? {} : { asked_ms: now - minutesAgo * 60000 }),
  });
  const first = hostOf(allKinds.status, {
    level: "attention",
    now,
    items: [question("KO-1", 1), question("KO-4", 4)],
  });
  const second = hostOf(
    allKinds.status,
    { level: "attention", now, items: [question("KO-2", 4), question("KO-9", null)] },
    "http://writer-2:7710",
  );
  render(<NeedsYou hosts={[first, second]} project="all" now={now} />);
  expect(rows().map((row) => within(row).getByText(/^KO-\d+$/).textContent)).toEqual(["KO-4", "KO-2", "KO-1", "KO-9"]);
});

test("the Failed chip keeps only KO-229; All brings the four back", () => {
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
  fireEvent.click(screen.getByRole("button", { name: "Failed 1" }));
  expect(rows().length).toBe(1);
  const [row] = rows();
  expect(within(row!).getByText("KO-229")).toBeTruthy();
  expect(within(row!).getByText("Verify command failed")).toBeTruthy();
  expect(within(row!).getByText("run #88 · strike 1 of 2")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Failed 1" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "All 4" }));
  expect(rows().length).toBe(4);
});

test("a failed row says what happened in plain words; the verbatim reason stays out of the row", () => {
  const [failed] = allKinds.attention.items.filter((item) => item.kind === "failed");
  const raw = failed!.reason as string;
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
  const row = rows().find((candidate) => candidate.getAttribute("data-kind") === "failed")!;
  expect(within(row).getByText("Verify command failed")).toBeTruthy();
  expect(within(row).getByText("run #88 · strike 1 of 2")).toBeTruthy();
  expect(within(row).queryByText(raw)).toBeNull();
  expect(within(row).queryByText(/test_store_surface/)).toBeNull();
  cleanup();

  const adjudicated: AttentionItem = {
    ...failed!,
    reason: "terminal adjudication: FAIL; branch task/ko-343-the-loop-runs-a-pool-of-worker preserved at 046d7d70f5e1",
  };
  render(<NeedsYou hosts={[hostOf(allKinds.status, { ...allKinds.attention, items: [adjudicated] })]} project="all" now={allKinds.status.now} />);
  const [only] = rows();
  expect(within(only!).getByText("Review adjudicated FAIL")).toBeTruthy();
  expect(within(only!).getByText("run #88 · strike 1 of 2 · task/ko-343-the-loop-runs-a-pool-of-worker @ 046d7d7")).toBeTruthy();
  expect(within(only!).queryByText(adjudicated.reason as string)).toBeNull();
  expect(within(only!).queryByText(/046d7d70f5e1/)).toBeNull();
});

test("six questions cap at four with Show all 6, expand, and a chip choice caps again", () => {
  const items: AttentionItem[] = Array.from({ length: 6 }, (_, index) => ({
    kind: "blocked",
    level: "attention",
    ticket: `KO-${300 + index}`,
    question: `Question ${index + 1}?`,
  }));
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  expect(rows().length).toBe(4);
  expect(chips()).toEqual(["All 6", "Questions 6"]);
  fireEvent.click(screen.getByRole("button", { name: "Show all 6" }));
  expect(rows().length).toBe(6);
  expect(screen.getByRole("button", { name: "Show fewer" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Questions 6" }));
  expect(rows().length).toBe(4);
  expect(screen.getByRole("button", { name: "Show all 6" })).toBeTruthy();
});

test("a question row with pr_url shows a PR #N anchor on its line; the fixture's rows without one show none", () => {
  const url = "https://github.com/o/r/pull/2170";
  const items: AttentionItem[] = [
    { kind: "blocked", level: "attention", ticket: "KO-335", run: 50, question: "Merge the PR?", pr_url: url },
    { kind: "blocked", level: "attention", ticket: "KO-336", run: 51, question: "Which branch?", pr_url: null },
  ];
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  const [withPr, without] = rows();
  const pr = within(withPr!).getByText("PR #2170") as HTMLAnchorElement;
  expect(pr.tagName).toBe("A");
  expect(pr.getAttribute("href")).toBe(url);
  expect(pr.getAttribute("target")).toBe("_blank");
  expect(pr.getAttribute("rel")).toBe("noopener noreferrer");
  expect(within(withPr!).getByText(/Merge the PR\?/)).toBeTruthy();
  expect(without!.querySelector("[data-pr]")).toBeNull();
  cleanup();

  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
  expect(document.querySelector("[data-pr]")).toBeNull();
});

test("every action button of a daemon without actions is disabled: wired ones name the opt-in, the rest not wired yet", () => {
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
  const actions = rows().flatMap((row) => within(row).getAllByRole("button"));
  expect(actions.map((button) => button.textContent)).toEqual([
    "Requeue",
    "Mark needs_spec",
    "Restart supervisor",
    "Kill run",
    "Requeue",
    "Answer",
    "Requeue",
  ]);
  for (const button of actions) {
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.getAttribute("title")).toBe(button.textContent! in ROUTES ? ACTIONS_OFF : NOT_WIRED);
  }
});

test("another project's selection leaves one quiet muted line and no band; one item reads singular", () => {
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="/srv/dev/other" now={allKinds.status.now} />);
  const lines = screen.getAllByText("Nothing needs you");
  expect(lines.length).toBe(1);
  const [line] = lines;
  expect(line!.tagName).toBe("P");
  expect(line!.className).toContain("text-muted");
  expect(line!.getAttribute("aria-label")).toBe("Needs you");
  expect(line!.previousElementSibling).toBeNull();
  expect(line!.nextElementSibling).toBeNull();
  expect(screen.queryByText("attention")).toBeNull();
  expect(screen.queryByRole("region", { name: "Needs you" })).toBeNull();
  cleanup();
  const one = { ...allKinds.attention, items: allKinds.attention.items.slice(0, 1) };
  render(<NeedsYou hosts={[hostOf(allKinds.status, one)]} project={allKinds.status.target} now={allKinds.status.now} />);
  expect(screen.getByText("thing needs you")).toBeTruthy();
  expect(rows().length).toBe(1);
});

test("a pr_open item adds a PRs chip that filters to it, and the total counts it with the rest", () => {
  const url = "https://github.com/o/r/pull/2170";
  const [question, ...rest] = allKinds.attention.items;
  const items: AttentionItem[] = [
    question!,
    { kind: "pr_open", level: "attention", ticket: "REL-120", run: 60, pr_url: url, reason: "review requested", asked_ms: allKinds.status.now - 600000 },
    ...rest,
  ];
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  expect(screen.getByText("5").hasAttribute("data-count")).toBe(true);
  expect(chips()).toEqual(["All 5", "Questions 1", "PRs 1", "Stale runs 1", "Failed 1", "Supervisor 1"]);
  expect(pills()).toEqual(["failed", "supervisor", "PR", "stale run"]);
  expect(screen.getByRole("button", { name: "Show all 5" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "PRs 1" }));
  expect(rows().length).toBe(1);
  const [row] = rows();
  expect(row!.getAttribute("data-kind")).toBe("pr_open");
  expect(within(row!).getByText("REL-120")).toBeTruthy();
  expect(within(row!).getByText(/^review requested/)).toBeTruthy();
  expect((within(row!).getByText("PR #2170") as HTMLAnchorElement).getAttribute("href")).toBe(url);
  expect(within(row!).getAllByRole("button").map((b) => b.textContent)).toEqual(["Open PR"]);
});

test("three failed attempts of one ticket are one row with a ×3 badge, counted once; its card lists every attempt and closes with a second click", () => {
  const strike = (run: number, reason: string): AttentionItem => ({
    kind: "failed",
    level: "attention",
    ticket: "KO-343",
    run,
    reason,
    attempt: 1,
    ended_ms: allKinds.status.now - (200 - run) * 60000,
  });
  const [question] = allKinds.attention.items;
  const items: AttentionItem[] = [
    strike(176, "terminal adjudication: FAIL; branch task/ko-343-the-loop-runs-a-pool-of-worker preserved at 046d7d70f5e1"),
    question!,
    strike(172, "verify failed before merge; branch task/ko-343-the-loop-runs-a-pool-of-worker preserved at 7f2e1a0b9c8d"),
    { kind: "failed", level: "attention", ticket: "KO-229", run: 88, reason: "verify failed: nope", attempt: 1, ended_ms: allKinds.status.now - 60000 },
    strike(174, "implementer made no commits; nothing to review"),
  ];
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  expect(screen.getByText("3").hasAttribute("data-count")).toBe(true);
  expect(chips()).toEqual(["All 3", "Questions 1", "Failed 2"]);
  expect(rows().map((row) => row.getAttribute("data-kind"))).toEqual(["failed", "failed", "blocked"]);
  const [grouped, single] = rows();
  expect(within(grouped!).getByText("Review adjudicated FAIL")).toBeTruthy();
  expect(within(grouped!).getByText("run #176 · strike 1 of 2 · task/ko-343-the-loop-runs-a-pool-of-worker @ 046d7d7")).toBeTruthy();
  expect(within(grouped!).getByText("24m")).toBeTruthy();
  const badge = grouped!.querySelector("[data-attempts]")!;
  expect(badge.textContent).toBe("×3");
  expect(badge.parentElement!.textContent).toBe("KO-343×3");
  expect(single!.querySelector("[data-attempts]")).toBeNull();
  expect(Array.from(grouped!.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Requeue", "Mark needs_spec"]);

  expect(document.querySelector("[data-attempts-card]")).toBeNull();
  fireEvent.click(within(grouped!).getByText("attempts ▾"));
  const card = grouped!.querySelector("[data-attempts-card]")!;
  expect(Array.from(card.querySelectorAll("[data-attempt]")).map((line) => line.textContent)).toEqual([
    "run #172 · Verify command failed · task/ko-343-the-loop-runs-a-pool-of-worker @ 7f2e1a0",
    "run #174 · Implementer exited without committing",
    "run #176 · Review adjudicated FAIL · task/ko-343-the-loop-runs-a-pool-of-worker @ 046d7d7",
  ]);
  expect(within(card as HTMLElement).queryByText(/046d7d70f5e1/)).toBeNull();
  expect(within(grouped!).getByText("hide attempts ▴")).toBeTruthy();
  fireEvent.click(within(grouped!).getByText("hide attempts ▴"));
  expect(document.querySelector("[data-attempts-card]")).toBeNull();
  expect(within(grouped!).getByText("attempts ▾")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Failed 2" }));
  expect(rows().length).toBe(2);
  expect(rows().map((row) => within(row).getByText(/^KO-/).textContent)).toEqual(["KO-343×3", "KO-229"]);
});

test("four failed items, three of one ticket and one of another: two failed rows, the count line and the Failed chip say 2, the older single leads and the grouped row wears ×3", () => {
  const strike = (ticket: string, run: number, reason: string): AttentionItem => ({
    kind: "failed",
    level: "attention",
    ticket,
    run,
    reason,
    attempt: 1,
    ended_ms: allKinds.status.now - (200 - run) * 60000,
  });
  const items: AttentionItem[] = [
    strike("KO-343", 172, "verify failed: a"),
    strike("KO-343", 174, "implementer made no commits; nothing to review"),
    strike("KO-229", 88, "verify failed: nope"),
    strike("KO-343", 176, "terminal adjudication: FAIL; branch task/ko-343-the-loop-runs-a-pool-of-worker preserved at 046d7d70f5e1"),
  ];
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  expect(screen.getByText("2").hasAttribute("data-count")).toBe(true);
  expect(screen.getByText("things need you")).toBeTruthy();
  expect(chips()).toEqual(["All 2", "Failed 2"]);
  expect(rows().map((row) => row.getAttribute("data-kind"))).toEqual(["failed", "failed"]);
  const [single, grouped] = rows();
  expect(within(single!).getByText("KO-229")).toBeTruthy();
  expect(single!.querySelector("[data-attempts]")).toBeNull();
  expect(within(grouped!).getByText("KO-343")).toBeTruthy();
  expect(grouped!.querySelector("[data-attempts]")!.textContent).toBe("×3");
});

test("a band row's age keeps counting between polls and realigns when the next answer lands", () => {
  const timers = captureIntervals();
  const t0 = allKinds.status.now;
  const item: AttentionItem = {
    kind: "stale_run",
    level: "attention",
    run: 91,
    ticket: "KO-232",
    phase: "reviewing",
    heartbeat_age_ms: 7_200_000,
  };
  const attention: Attention = { level: "attention", now: t0, items: [item] };
  setSystemTime(t0);
  try {
    const view = render(
      <NeedsYou hosts={[hostOf(allKinds.status, attention, "http://writer:7710", t0)]} project="all" now={t0} />,
    );
    expect(screen.getByText("No heartbeat for 2h 00m while reviewing")).toBeTruthy();
    setSystemTime(t0 + 60_000);
    act(() => timers.fire());
    expect(screen.getByText("No heartbeat for 2h 01m while reviewing")).toBeTruthy();
    // The next poll lands with a fresh 2h age: the row reads it as sent.
    view.rerender(
      <NeedsYou hosts={[hostOf(allKinds.status, attention, "http://writer:7710", t0 + 60_000)]} project="all" now={t0 + 60_000} />,
    );
    expect(screen.getByText("No heartbeat for 2h 00m while reviewing")).toBeTruthy();
  } finally {
    setSystemTime();
    timers.restore();
  }
});

test("an open attempts card closes when a question row opens its thread: one card at a time", () => {
  const ledgers: Ledgers = {
    "writer:7710": {
      rows: [],
      threads: { "KO-240": [{ at: allKinds.status.now - 1000, run: 50, ticket: "KO-240", kind: "note", source: "loop", text: "Which branch?" }] },
      absent: false,
    },
  };
  const question: AttentionItem = { kind: "blocked", level: "attention", ticket: "KO-240", run: 50, question: "Which branch?" };
  const items: AttentionItem[] = [
    { kind: "failed", level: "attention", ticket: "KO-343", run: 172, reason: "verify failed: a", attempt: 1 },
    question,
    { kind: "failed", level: "attention", ticket: "KO-343", run: 176, reason: "verify failed: b", attempt: 2 },
  ];
  render(
    <NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} ledgers={ledgers} />,
  );
  const [grouped, blocked] = rows();
  fireEvent.click(within(grouped!).getByText("attempts ▾"));
  expect(grouped!.querySelector("[data-attempts-card]")).toBeTruthy();
  fireEvent.click(within(blocked!).getByText("thread ▾"));
  expect(grouped!.querySelector("[data-attempts-card]")).toBeNull();
  expect(blocked!.querySelector("[data-thread]")).toBeTruthy();
  fireEvent.click(within(grouped!).getByText("attempts ▾"));
  expect(blocked!.querySelector("[data-thread]")).toBeNull();
  expect(document.querySelectorAll("[data-attempts-card]").length).toBe(1);
});
