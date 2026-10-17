import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { runTurnsSchema, transcriptSchema, type RunDetail } from "../../src/lib/contract.gen";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const detail = { ...served, run: { ...served.run, id: 12, host: "writer-host" } };
const turn = runTurnsSchema.parse({ turns: [{ id: 42, role: "implement", label: "claude opus", route: "primary",
  session_id: null, ended_ms: 190000, seconds: 189, exit_status: 0, timed_out: false,
  input_tokens: null, output_tokens: null, cost_usd: null }] }).turns[0]!;
const entry = { tool: null, call: null, summary: null, failed: false, cut: false };
const transcript = transcriptSchema.parse({ entries: [
  { ...entry, speaker: "user", text: "Fix the sweep and preserve the ownership check.", at_ms: 1000 },
  { ...entry, speaker: "assistant", text: "I will reproduce test_sweep before editing.", at_ms: 2000 },
  { ...entry, speaker: "command", tool: "Read", call: "read", summary: "Inspect lease ownership", text: "Read sweep.py", at_ms: 3000 },
  { ...entry, speaker: "tool", tool: "Read", call: "read", text: "Lease ownership guard", at_ms: 4000 },
  { ...entry, speaker: "command", tool: "Bash", call: "bash", summary: "Run focused tests", text: "python3 -m unittest tests.test_lease", at_ms: 5000 },
  { ...entry, speaker: "tool", tool: "Bash", call: "bash", text: "FAIL: lease remains held", failed: true, at_ms: 6000 },
  { ...entry, speaker: "error", text: "529 overloaded", at_ms: 7000 },
  { ...entry, speaker: "command", tool: "Edit", call: "edit", summary: "Guard the lease release", text: "Replace stale lease release with guarded release.", at_ms: 8000 },
  { ...entry, speaker: "tool", tool: "Edit", call: "edit", text: "test_sweep now passes. Verified test_sweep again.", at_ms: 9000 },
  { ...entry, speaker: "command", tool: "Bash", call: "verify", summary: "Verify the patch", text: "ruff check .", at_ms: 10000 },
  { ...entry, speaker: "tool", tool: "Bash", call: "verify", text: "All checks passed!", at_ms: 11000 },
] });

async function mount(failures = true) {
  const polls = fleet({ items: [] }, { items: [] });
  const fake = fakeDeps(async url => {
    if (url === `${DEMO}/runs/12`) return Response.json(detail);
    if (url === `${DEMO}/runs/12/turns`) return Response.json({ turns: [turn] });
    if (url === `${DEMO}/runs/12/turns/42/transcript`) return Response.json(failures ? transcript : {
      entries: transcript.entries.filter(row => row.speaker !== "error").map(row => ({ ...row, failed: false })),
    });
    return polls(url);
  });
  window.location.hash = "#/runs/demo/12/turns/42";
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => { happyDOM.setURL(`${DEMO}/next.html`); });
afterAll(() => { happyDOM.setURL("about:blank"); });
afterEach(() => { cleanup(); window.location.hash = ""; });
const panel = () => screen.getByRole("list", { name: "Turn transcript" });
const rows = () => within(panel()).getAllByRole("listitem");
const search = async (value: string) => {
  await act(async () => { fireEvent.change(screen.getByRole("searchbox", { name: "Search this turn" }), { target: { value } }); });
};

test("whole-turn counts include the prompt and End while Errors keeps only failed calls and error rows", async () => {
  await mount();
  expect(screen.getByText("8 entries · 2 errors").getAttribute("aria-live")).toBe("polite");
  const filters = within(screen.getByRole("group", { name: "Show" }));
  for (const name of ["All 8", "Said 1", "Tools 5", "Errors 2"]) {
    expect(filters.getByRole("button", { name }).getAttribute("aria-pressed")).toBe(name === "All 8" ? "true" : "false");
  }
  expect(rows()).toHaveLength(8);
  await act(async () => { fireEvent.click(filters.getByRole("button", { name: "Errors 2" })); });
  expect(rows()).toHaveLength(2);
  expect(rows()[0]!.textContent).toContain("FAIL: lease remains held");
  expect(rows()[1]!.textContent).toContain("529 overloaded");
  expect(within(panel()).queryByText("End")).toBeNull();
});

test("case-insensitive search counts occurrences, marks them and opens matching output until manually closed", async () => {
  await mount();
  const edit = rows().find(row => within(row).queryByText("Edit"))!.querySelector("details")!;
  expect(edit.open).toBe(false);
  await search("Test_Sweep");
  expect(screen.getByText("3 matches in 2 entries")).toBeTruthy();
  expect(rows()).toHaveLength(2);
  const matchingFold = panel().querySelector("details")!;
  expect(matchingFold.open).toBe(true);
  expect(Array.from(panel().querySelectorAll("mark"), mark => mark.textContent)).toEqual(["test_sweep", "test_sweep", "test_sweep"]);
  await act(async () => { matchingFold.open = false; fireEvent(matchingFold, new Event("toggle")); });
  expect(matchingFold.open).toBe(false);
  await act(settle);
  expect(matchingFold.open).toBe(false);
  await search("zzz");
  expect(screen.getByText("Nothing in this turn matches “zzz”.")).toBeTruthy();
  expect(screen.getByText("0 matches in 0 entries")).toBeTruthy();
});

test("jump opens and focuses the first failed call, including when the current search hides it", async () => {
  await mount();
  const failed = rows().find(row => row.textContent?.includes("FAIL: lease remains held"))!;
  const fold = failed.querySelector("details")!;
  await act(async () => { fold.open = false; fireEvent(fold, new Event("toggle")); });
  await search("test_sweep");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Jump to first error" })); });
  const target = rows().find(row => row.textContent?.includes("FAIL: lease remains held"))!;
  expect(target.querySelector("details")!.open).toBe(true);
  expect(document.activeElement).toBe(target);
});

test("jump is disabled when the turn has no failed calls or errors", async () => {
  await mount(false);
  expect((screen.getByRole("button", { name: "Jump to first error" }) as HTMLButtonElement).disabled).toBe(true);
});

test("Expand all opens every fold and Collapse all closes even the failed call", async () => {
  await mount();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Expand all" })); });
  expect(panel().querySelectorAll("details")).toHaveLength(5);
  expect(Array.from(panel().querySelectorAll("details"), fold => fold.open)).toEqual([true, true, true, true, true]);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Collapse all" })); });
  expect(Array.from(panel().querySelectorAll("details"), fold => fold.open)).toEqual([false, false, false, false, false]);
  expect(screen.getByRole("button", { name: "Expand all" })).toBeTruthy();
});
