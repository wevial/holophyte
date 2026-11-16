import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RoundCriterion, RunCriterion, RunDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { Criteria } from "../../src/app/components/Criteria";
import { criteriaRows } from "../../src/lib/criteria";
import { criteriaClause } from "../../src/app/run";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, run, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const criteria: RunCriterion[] = [
  { id: "C1", n: 1, text: "Silent leases are requeued.", status: "met", note: "tests/test_a.py::test_one", run: 12, round: 1 },
  { id: "C2", n: 2, text: "Fresh leases remain active.", status: "met", note: "tests/test_a.py::test_two", run: 12, round: 1 },
  { id: "C3", n: 3, text: "The sweep records its work.", status: "met", note: "tests/test_a.py::test_three", run: 12, round: 1 },
  { id: "C4", n: 4, text: "The row names sweep.", status: "not_met", note: "the row names loop", run: 12, round: 1 },
];
const rounds: RunDetail["rounds"] = [
  { ...served.rounds[0]!, round: 1 },
  { ...served.rounds[0]!, round: 2, ended_ms: null, verdict: "", findings: [], instructions: [], operator_notes: [] },
];

async function mount(standing = criteria, judgedRounds = rounds, precheck = served.precheck) {
  const polls = fleet({ items: [], runs: [{ ...run(12, "DEMO-12"), phase: "reviewing", round: 1, max_rounds: 3 }] }, { items: [] });
  const fetch: Fetch = async (url) => url === `${DEMO}/runs/12`
    ? Response.json({ ...served, run: { ...served.run, id: 12, ticket: "DEMO-12", phase: "reviewing", max_rounds: 3 }, rounds: judgedRounds, criteria: standing, precheck })
    : polls(url);
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  for (let tick = 0; tick < 6; tick += 1) await act(settle);
}

const judged = (n: number, status: RoundCriterion["status"]): RoundCriterion => ({ id: `C${n}`, n, status, note: "" });
const judgedRound = (number: number, statuses: RoundCriterion["status"][]): RunDetail["rounds"][number] => ({
  ...served.rounds[0]!, round: number, started_ms: served.rounds[0]!.started_ms + number, findings: [], instructions: [], operator_notes: [], criteria: statuses.map((status, index) => judged(index + 1, status)),
});

/** A merged run whose pre-check judged C1, C3 and C4, then rounds 1 and 2 and the final check judged all four. */
const merged: RunDetail = {
  ...served,
  run: { ...served.run, id: 12, ticket: "DEMO-12", phase: "done", outcome: "merged", max_rounds: 2 },
  events: [],
  precheck: { sha: "c".repeat(40), criteria: [1, 3, 4].map((n) => ({ id: `C${n}`, n, witnessed: n !== 3, note: "" })) },
  rounds: [
    judgedRound(1, ["not_met", "met", "unwitnessed", "met"]),
    judgedRound(2, ["met", "met", "met", "not_met"]),
    judgedRound(3, ["met", "met", "met", "not_met"]),
  ],
  criteria,
};

/** A run that failed at its final check, which left every criterion unwitnessed. */
const failedFinal: RunDetail = {
  ...merged,
  run: { ...merged.run, outcome: "failed", outcome_reason: "terminal adjudication: FAIL" },
  precheck: null,
  rounds: [judgedRound(1, ["met", "met", "met", "not_met"]), judgedRound(3, ["unwitnessed", "unwitnessed", "unwitnessed", "unwitnessed"])],
};

async function mountRun(detail: RunDetail) {
  const polls = fleet({ items: [] }, { items: [] });
  const fetch: Fetch = async (url) => url === `${DEMO}/runs/12` ? Response.json(detail) : polls(url);
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  for (let tick = 0; tick < 6; tick += 1) await act(settle);
}

/** Each criterion row's round marks: one entry per column, the mark's name or null for an empty slot. */
const markRows = () => Array.from(block().querySelectorAll("[data-round-marks]"), (marks) =>
  Array.from(marks.children, (slot) => slot.querySelector("[role=img]")?.getAttribute("aria-label") ?? null));

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => { cleanup(); window.location.hash = ""; });
const block = () => screen.getByRole("region", { name: "Acceptance criteria" });
const verdict = () => within(screen.getByRole("main")).getByRole("status").textContent;

test("the Run page counts reviewed criteria, names their marks and waits only for the unmet row's active round", async () => {
  await mount();
  const list = within(block());
  expect(list.getByText("3 of 4 met")).toBeTruthy();
  const rows = within(list.getAllByRole("list")[0]!).getAllByRole("listitem");
  expect(rows.map((row) => within(row).getAllByRole("img")[0]!).map((mark) => [mark.getAttribute("aria-label"), mark.getAttribute("data-tone")])).toEqual([
    ["met", "live"], ["met", "live"], ["met", "live"], ["unmet", "need"],
  ]);
  expect(rows).toHaveLength(4);
  expect(within(rows[0]!).getByText("C1")).toBeTruthy();
  expect(within(rows[0]!).getByText("Silent leases are requeued.")).toBeTruthy();
  expect(within(rows[3]!).getByText("waits on round 2")).toBeTruthy();
  expect(list.getAllByText("waits on round 2")).toHaveLength(1);
  expect(verdict()).toBe("Reviewing round 2 of 3. Three of four criteria are met.");
});

test("Evidence opens from a closed fold and distinguishes test witnesses from round findings", async () => {
  await mount();
  const fold = block().querySelector("details")!;
  expect(fold.open).toBe(false);
  const summary = fold.querySelector("summary")!;
  expect(summary.textContent?.replace(/\s+/g, " ")).toBe("Evidence 3 tests · 1 finding");
  await act(async () => void fireEvent.click(summary));
  expect(fold.open).toBe(true);
  expect(within(fold).getAllByRole("listitem")[0]?.textContent).toBe("C1 · tests/test_a.py::test_one");
  expect(within(fold).getAllByRole("listitem")[3]?.textContent).toBe("C4 · round 1: the row names loop");
});

test("an unwitnessed criterion has a clock mark and an unjudged run has an empty notice without a verdict count", async () => {
  await mount([{ ...criteria[3]!, status: "unwitnessed" }]);
  expect(within(block()).getByRole("img", { name: "not yet witnessed" }).getAttribute("data-tone")).toBe("wait");
  cleanup();
  await mount([], [], null);
  expect(within(block()).getByText("No review has judged the criteria yet.")).toBeTruthy();
  expect(within(block()).queryByRole("list")).toBeNull();
  expect(verdict()).toBe("Reviewing round 2 of 3.");
});

test("a live retry waits for a new judgment even when its earlier run marked the criterion met", async () => {
  await mount([{ ...criteria[0]!, run: 11 }], []);
  expect(within(block()).getByRole("img", { name: "met" })).toBeTruthy();
  expect(within(block()).getByText("waits on round 1")).toBeTruthy();
});

test("criteria sentences distinguish all, none and partial counts and spell numbers through ten", () => {
  expect(criteriaClause(criteria.map((criterion) => ({ ...criterion, status: "met" })))).toBe("All four criteria are met.");
  expect(criteriaClause(criteria.map((criterion) => ({ ...criterion, status: "unwitnessed" })))).toBe("None of four criteria is met yet.");
  const eleven = Array.from({ length: 11 }, (_, index) => ({ ...criteria[0]!, n: index + 1, status: index < 10 ? "met" as const : "not_met" as const }));
  expect(criteriaClause(eleven)).toBe("Ten of 11 criteria are met.");
  expect(criteriaClause(eleven.map((criterion) => ({ ...criterion, status: "met" })))).toBe("All 11 criteria are met.");
});


test("Criteria labels current criteria with bold ids before their texts", () => {
  const current = [{ id: "C1", text: "first" }, { id: "C2", text: "second" }, { id: "C4", text: "fourth" }];
  render(<Criteria rows={criteriaRows(current, [{ id: "C4", status: "not_met" }, { id: "C1", status: "met" }], true)} run={12} />);
  const rows = within(block()).getAllByRole("listitem");
  expect(rows.map((row) => row.querySelector("strong")?.textContent)).toEqual(["C1", "C2", "C4"]);
  expect(rows.map((row) => row.textContent)).toEqual(["C1first", "C2second", "C4fourth"]);
  expect(rows.map((row) => row.querySelector("[data-standing]")?.getAttribute("data-standing"))).toEqual(["met", "open", "unmet"]);
  expect(block().textContent).not.toContain("AC");
});

test("a criterion's row ends with a mark per column that judged it, in order, named by column and verdict, and grouped by those names", async () => {
  await mountRun(merged);
  expect(markRows()[0]).toEqual(["pre-check · met", "round 1 · not met", "round 2 · met", "final check · met"]);
  const c1 = within(block()).getAllByRole("listitem")[0]!;
  expect(within(c1).getByRole("group").getAttribute("aria-label")).toBe("pre-check · met, round 1 · not met, round 2 · met, final check · met");
});

test("a criterion the pre-check did not judge keeps an empty first slot, its round 1 mark in the same column as the judged rows'", async () => {
  await mountRun(merged);
  const rows = markRows();
  expect(rows.map((row) => row.length)).toEqual([4, 4, 4, 4]);
  expect(rows[1]).toEqual([null, "round 1 · met", "round 2 · met", "final check · met"]);
  expect(rows[2]![0]).toBe("pre-check · unwitnessed");
  expect(within(within(block()).getAllByRole("listitem")[1]!).getAllByRole("img")[1]!.getAttribute("aria-label")).toBe("round 1 · met");
});

test("a round mark's tooltip reads its name on hover and keyboard focus, and goes on pointer leave and blur", async () => {
  await mountRun(merged);
  const mark = within(block()).getByRole("img", { name: "round 1 · not met" });
  const tip = () => screen.queryByRole("tooltip");
  expect(tip()).toBeNull();
  await act(async () => void fireEvent.pointerEnter(mark, { pointerType: "mouse" }));
  await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
  expect(tip()!.textContent).toBe("round 1 · not met");
  await act(async () => void fireEvent.pointerLeave(mark, { pointerType: "mouse", clientX: 900, clientY: 900 }));
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
  expect(tip()).toBeNull();
  await act(async () => {
    fireEvent.keyDown(document.body, { key: "Tab" });
    mark.focus();
  });
  expect(tip()!.textContent).toBe("round 1 · not met");
  await act(async () => mark.blur());
  expect(tip()).toBeNull();
});

test("a run failed at its final check with every cell unwitnessed ends every row on a final check mark drawn in the ask tone", async () => {
  await mountRun(failedFinal);
  const rows = within(block()).getAllByRole("listitem").filter((row) => row.querySelector("[data-round-marks]"));
  expect(rows).toHaveLength(4);
  for (const row of rows) {
    const last = within(row).getAllByRole("img").at(-1)!;
    expect(last.getAttribute("aria-label")).toBe("final check · unwitnessed");
    expect(last.style.color).toBe("var(--ask)");
  }
});

test("the criteria head reads Criteria with its met count beside it, in sentence case without caps or tracking", async () => {
  await mountRun(merged);
  const head = within(block()).getByText("3 of 4 met").parentElement!;
  expect(head.textContent).toBe("Criteria3 of 4 met");
  for (const el of [head, ...head.querySelectorAll("*")]) expect(Array.from(el.classList).filter((name) => name === "uppercase" || name.startsWith("tracking"))).toEqual([]);
});

test("before any review returns, the pre-check's verdicts show as marks on rows for the criteria it judged", async () => {
  await mountRun({ ...merged, run: { ...merged.run, outcome: null, phase: "reviewing" }, rounds: [], criteria: [] });
  const rows = within(block()).getAllByRole("listitem");
  expect(rows.map((row) => row.querySelector("strong")?.textContent)).toEqual(["C1", "C3", "C4"]);
  expect(markRows()).toEqual([["pre-check · met"], ["pre-check · unwitnessed"], ["pre-check · met"]]);
});
