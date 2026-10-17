import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunCriterion, RunDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { Criteria } from "../../src/next/components/Criteria";
import { criteriaRows } from "../../src/lib/criteria";
import { criteriaClause } from "../../src/next/run";
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

async function mount(standing = criteria, judgedRounds = rounds) {
  const polls = fleet({ items: [], runs: [{ ...run(12, "DEMO-12"), phase: "reviewing", round: 1, max_rounds: 3 }] }, { items: [] });
  const fetch: Fetch = async (url) => url === `${DEMO}/runs/12`
    ? Response.json({ ...served, run: { ...served.run, id: 12, ticket: "DEMO-12", phase: "reviewing", max_rounds: 3 }, rounds: judgedRounds, criteria: standing })
    : polls(url);
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  for (let tick = 0; tick < 6; tick += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => { cleanup(); window.location.hash = ""; });
const block = () => screen.getByRole("region", { name: "Acceptance criteria" });
const verdict = () => within(screen.getByRole("main")).getByRole("status").textContent;

test("the Run page counts reviewed criteria, names their marks and waits only for the unmet row's active round", async () => {
  await mount();
  const list = within(block());
  expect(list.getByText("3 of 4 met")).toBeTruthy();
  expect(list.getAllByRole("img").map((mark) => [mark.getAttribute("aria-label"), mark.getAttribute("data-tone")])).toEqual([
    ["met", "live"], ["met", "live"], ["met", "live"], ["unmet", "need"],
  ]);
  const rows = within(list.getAllByRole("list")[0]!).getAllByRole("listitem");
  expect(rows).toHaveLength(4);
  expect(within(rows[0]!).getByText("C1")).toBeTruthy();
  expect(within(rows[0]!).getByText("Silent leases are requeued.")).toBeTruthy();
  expect(within(rows[3]!).getByText("waits on round 2")).toBeTruthy();
  expect(list.getAllByText("waits on round 2")).toHaveLength(1);
  expect(verdict()).toBe("Reviewing round 2 of 3. Three of four criteria are met. Nothing needs you.");
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
  await mount([], []);
  expect(within(block()).getByText("No review has judged the criteria yet.")).toBeTruthy();
  expect(within(block()).queryByRole("list")).toBeNull();
  expect(verdict()).toBe("Reviewing round 2 of 3. Nothing needs you.");
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
