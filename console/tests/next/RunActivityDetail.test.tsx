import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RoundFinding, RunDetail, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, SERVE } from "./needsFleet";

type Round = RunDetail["rounds"][number];
type Criterion = Round["criteria"][number];

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();

const phase = (hour: number, minute: number, summary: string) => ({ at: at(hour, minute), kind: "phase_change", summary });
const criterion = (n: number, status: Criterion["status"], note = ""): Criterion => ({ id: `C${n}`, n, status, note });
const allOf = (status: Criterion["status"]) => [1, 2, 3].map((n) => criterion(n, status));
const round = (number: number, from: [number, number], to: [number, number], fields: Partial<Round>): Round => ({
  round: number, started_ms: at(...from) + 1, ended_ms: at(...to), verdict: "changes_requested", reviewer_model: "codex-sol-high",
  findings: [], instructions: [], operator_notes: [], criteria: [], ...fields,
});
const finding = (severity: string, path: string, line: number, message: string, fields: Partial<RoundFinding> = {}): RoundFinding => ({
  severity, path, line, message, ...fields,
});

const KEYBOARD = finding(
  "p2", "/home/reviewer/candidate/console/src/next/components/TomlEditor.tsx", 88,
  "- **[P2] Preserve keyboard paging** — [TomlEditor.tsx:88](/home/reviewer/candidate/console/src/next/components/TomlEditor.tsx:88). Reproduced in Chromium.",
);
const EXPORT = finding("p2", "console/src/next/tomlTokens.ts", 92, "- **[P2] Restore the `tomlTokens(line)` export** — the trim removed it.", { criterion: "C1" });
const WITNESS = finding("p2", "console/tests/next/RawToml.test.tsx", 213, "Witness the line-3 save refusal");

const base: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 12, ticket: "DEMO-12", title: "Edit the raw TOML", phase: "done", started_ms: at(16, 0), max_rounds: 2 },
  findings: [],
  steer_notes: [],
  criteria: [],
  precheck: null,
};

/** Run 12 merged after two review rounds and a passed final check. */
const finished: RunDetail = {
  ...base,
  run: { ...base.run, outcome: "merged", ended_ms: at(16, 40), merge_sha: "abc1234def", commit_url: "https://github.test/demo/commit/abc1234def" },
  precheck: {
    sha: "c".repeat(40),
    criteria: [
      { id: "C1", n: 1, witnessed: true, note: "" },
      { id: "C2", n: 2, witnessed: true, note: "" },
      { id: "C3", n: 3, witnessed: false, note: "" },
    ],
  },
  events: [
    phase(16, 0, "claimed -> working"),
    phase(16, 10, "working -> verifying"),
    phase(16, 11, "verifying -> reviewing: round 1 review"),
    phase(16, 14, "reviewing -> addressing"),
    phase(16, 20, "addressing -> verifying"),
    phase(16, 21, "verifying -> reviewing: round 2 review"),
    phase(16, 24, "reviewing -> addressing"),
    phase(16, 28, "addressing -> verifying"),
    phase(16, 29, "verifying -> reviewing: terminal adjudication"),
    phase(16, 31, "reviewing -> merge_gate"),
  ],
  rounds: [
    round(1, [16, 11], [16, 14], {
      findings: [KEYBOARD, EXPORT, WITNESS],
      criteria: [criterion(1, "not_met", "export is absent"), criterion(2, "met"), criterion(3, "unwitnessed")],
    }),
    round(2, [16, 21], [16, 24], { findings: [finding("nit", "console/src/next/other.ts", 3, "Name the helper")], criteria: allOf("met") }),
    round(3, [16, 29], [16, 30], { verdict: "pass", reviewer_model: "claude-adjudicate", criteria: allOf("met") }),
  ],
};

/** Run 12 failed at its final check with every criterion unwitnessed, then parked on the steer cap. */
const failedFinal: RunDetail = {
  ...base,
  run: {
    ...base.run, max_rounds: 1, outcome: "failed", failure_kind: "review_route", ended_ms: at(16, 40),
    outcome_reason: "terminal adjudication: FAIL\nthe judge's reply carried no CRITERION lines",
  },
  events: [
    phase(16, 0, "claimed -> working"),
    phase(16, 10, "working -> verifying"),
    phase(16, 11, "verifying -> reviewing: round 1 review"),
    phase(16, 14, "reviewing -> addressing"),
    phase(16, 20, "addressing -> verifying"),
    phase(16, 21, "verifying -> reviewing: terminal adjudication"),
    { at: at(16, 25), kind: "steer_cap_park", summary: "the review rounds are used up after an amendment" },
  ],
  rounds: [
    round(1, [16, 11], [16, 14], { findings: [WITNESS], criteria: [criterion(1, "met"), criterion(2, "met"), criterion(3, "not_met")] }),
    round(2, [16, 21], [16, 24], { reviewer_model: "claude-adjudicate", criteria: allOf("unwitnessed") }),
  ],
};

const THREAD_URL = "https://github.test/demo/pull/7#discussion_r1";

/** Run 12 sent back to the babysitter after it answered two review threads. */
const handedOver: RunDetail = {
  ...base,
  run: { ...base.run, outcome: "abandoned", ended_ms: at(16, 40), outcome_reason: "sent back to the babysitter; the next claim resumes the candidate on its pull request" },
  events: [phase(16, 0, "claimed -> working"), { at: at(16, 30), kind: "babysit_step", summary: "threads" }],
  rounds: [
    round(1, [16, 30], [16, 33], {
      findings: [
        finding("p2", "console/src/next/run.ts", 9, "Relative imports reach outside the folder.", {
          kind: "thread", summary: "Relative imports reach outside the folder.", verdict: "ADDRESS", url: THREAD_URL,
          reply: "Addressed in 4eb4b064ae2f1ecac67a741a686260ca533a1582: imports use the alias",
        }),
        finding("p2", "console/src/next/components/AdminListTable.tsx", 80, "Search leaves the date column out.", {
          kind: "thread", summary: "Search leaves the date column out.", verdict: "DECLINE", url: `${THREAD_URL}2`,
          reply: "Declined: The ticket limits search to text columns.",
        }),
      ],
    }),
  ],
};

/** DEMO answering `/runs/12` with `detail`, and DEMO-12's ticket listing runs 12 and 13. */
function runFetch(detail: RunDetail): Fetch {
  const polls = fleet({ items: [] }, { items: [] });
  const runs = [12, 13].map((run) => ({ ...servedTicket.runs[0]!, run }));
  return async (url) => {
    if (url === `${DEMO}/runs/12`) return Response.json(detail);
    if (url === `${DEMO}/runs/12/asks`) return Response.json({ run: 12, ticket: "DEMO-12", pr_url: null, asks: [] });
    if (url === `${DEMO}/tickets/DEMO-12`) return Response.json({ ...servedTicket, ticket: "DEMO-12", runs });
    return polls(url);
  };
}

async function mount(detail: RunDetail) {
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(runFetch(detail));
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let turn = 0; turn < 6; turn += 1) await act(settle);
}

const progress = () => screen.getByRole("region", { name: "Progress" });
const grid = () => within(progress()).queryByRole("table", { name: "Criteria by round" });
const columnCells = (table: HTMLElement, column: number) => Array.from(table.querySelectorAll("tbody tr"), (row) => row.querySelectorAll("td")[column]!);

function group(title: string): HTMLElement {
  const found = Array.from(progress().querySelectorAll<HTMLElement>("[data-group]")).find((li) => li.querySelector("[data-group-title] b")?.textContent === title);
  if (found == null) throw new Error(`no group titled ${title}`);
  return found;
}

/** The group's finding rows, opening the group first when it is folded. */
async function openRows(title: string): Promise<HTMLElement[]> {
  const toggle = group(title).querySelector("button[aria-expanded]")!;
  if (toggle.getAttribute("aria-expanded") === "false") await act(async () => void fireEvent.click(toggle));
  return Array.from(group(title).querySelectorAll<HTMLElement>("[data-activity-finding]"));
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the criteria grid has a column for the pre-check, each review round and the final check, and names each cell by its status and note", async () => {
  await mount(finished);
  const table = grid()!;
  expect(Array.from(table.querySelectorAll("thead th"), (th) => th.textContent)).toEqual(["pre-check", "round 1", "round 2", "final"]);
  expect(Array.from(table.querySelectorAll("tbody th"), (th) => th.textContent)).toEqual(["C1", "C2", "C3"]);
  expect(columnCells(table, 1)[0]!.getAttribute("aria-label")).toBe("not met: export is absent");
  expect(columnCells(table, 0)[2]!.getAttribute("aria-label")).toBe("unwitnessed");
  expect(within(table).getAllByRole("cell", { name: "not met: export is absent" })).toHaveLength(1);
  expect(table.querySelectorAll("[data-failed]")).toHaveLength(0);
});

test("a failed final check's column reads unwitnessed in every cell under the failed outline, and a run that judged no criteria has no grid", async () => {
  await mount(failedFinal);
  const table = grid()!;
  const final = table.querySelector("thead th[data-failed]")!;
  expect(final.textContent).toBe("final");
  expect(columnCells(table, 1).map((cell) => [cell.getAttribute("aria-label"), cell.hasAttribute("data-failed")])).toEqual([
    ["unwitnessed", true],
    ["unwitnessed", true],
    ["unwitnessed", true],
  ]);
  cleanup();
  await mount({ ...handedOver, rounds: handedOver.rounds.map((each) => ({ ...each, criteria: [] })) });
  expect(progress().querySelector("[data-group]")).not.toBeNull();
  expect(grid()).toBeNull();
});

test("a review round's header marks each criterion it left not met or unwitnessed after its findings count", async () => {
  await mount(finished);
  const title = group("Round 1").querySelector("[data-group-title]")!.textContent!;
  expect(title.startsWith("Round 1 · 3 should-fix")).toBe(true);
  expect(title.indexOf("C1 not met")).toBeGreaterThan(title.indexOf("3 should-fix"));
  expect(title.indexOf("C3 unwitnessed")).toBeGreaterThan(title.indexOf("C1 not met"));
  expect(title).not.toContain("C2");
});

test("a round's finding reads its severity, plain title, that the next round no longer raises it, and its repository path", async () => {
  await mount(finished);
  const rows = await openRows("Round 1");
  expect(rows).toHaveLength(3);
  const keyboard = rows.find((row) => row.textContent!.includes("Preserve keyboard paging"))!;
  expect(keyboard.querySelector("[data-pill]")!.textContent).toBe("P2");
  expect(keyboard.querySelector("[data-finding-title]")!.textContent).toBe("Preserve keyboard paging");
  expect(keyboard.querySelector("[data-outcome]")!.textContent).toBe("not raised again");
  expect(keyboard.querySelector("[data-location]")!.textContent).toBe("console/src/next/components/TomlEditor.tsx:88");
  for (const row of rows) {
    expect(row.textContent).not.toContain("/home/reviewer");
    expect(row.textContent).not.toContain("**");
  }
});

test("a last review round's finding is not read as no longer raised when the failed final check after it judged only criteria", async () => {
  await mount(failedFinal);
  const [witness] = await openRows("Round 1");
  expect(witness!.textContent).toContain("Witness the line-3 save refusal");
  expect(witness!.querySelector("[data-outcome]")).toBeNull();
});

test("More shows a finding's text beyond its title and turns to Less; a finding that is only its title has no More", async () => {
  await mount(finished);
  const rows = await openRows("Round 1");
  const keyboard = rows.find((row) => row.textContent!.includes("Preserve keyboard paging"))!;
  expect(keyboard.textContent).not.toContain("Reproduced in Chromium.");
  await act(async () => void fireEvent.click(within(keyboard).getByRole("button", { name: "More" })));
  expect(keyboard.textContent).toContain("Reproduced in Chromium.");
  expect(within(keyboard).getByRole("button", { name: "Less" })).toBeTruthy();
  const witness = rows.find((row) => row.textContent!.includes("Witness the line-3 save refusal"))!;
  expect(within(witness).queryByRole("button", { name: "More" })).toBeNull();
});

test("a review thread reads Addressed with its fix's short sha or Declined with the reply's reason, its place linking to the thread", async () => {
  await mount(handedOver);
  const [addressed, declined] = await openRows("Review threads");
  expect(addressed!.querySelector("[data-outcome]")!.textContent).toBe("Addressed");
  expect(addressed!.querySelector("[data-outcome-detail]")!.textContent).toBe("4eb4b06");
  expect(within(addressed!).getByRole("link", { name: "console/src/next/run.ts:9" }).getAttribute("href")).toBe(THREAD_URL);
  expect(declined!.querySelector("[data-outcome]")!.textContent).toBe("Declined");
  expect(declined!.querySelector("[data-outcome-detail]")!.textContent).toBe("The ticket limits search to text columns.");
});

test("a run failed at its final check ends on a card saying what the check left open, the steer cap and the next run, its reason folded", async () => {
  await mount(failedFinal);
  expect(within(progress()).queryByRole("region", { name: "Now" })).toBeNull();
  const card = within(progress()).getByRole("region", { name: "How the run ended" });
  const text = card.textContent!;
  for (const words of ["Failed at the final check", "3 of 3", "2 of 3 met", "steer cap", "run 13"]) expect(text).toContain(words);
  expect(text).not.toContain("terminal adjudication: FAIL");
  await act(async () => void fireEvent.click(within(card).getByRole("button", { name: "raw reason" })));
  expect(card.textContent).toContain("terminal adjudication: FAIL");
});

test("a run sent back to the babysitter ends on Handed to the babysitter, and a merged run on its short sha linked to the commit", async () => {
  await mount(handedOver);
  expect(within(progress()).getByRole("region", { name: "How the run ended" }).querySelector("[data-end-title]")!.textContent).toBe("Handed to the babysitter");
  cleanup();
  await mount(finished);
  const card = within(progress()).getByRole("region", { name: "How the run ended" });
  expect(card.querySelector("[data-end-title]")!.textContent).toMatch(/^Merged as abc1234 at /);
  expect(within(card).getByRole("link", { name: "abc1234" }).getAttribute("href")).toBe("https://github.test/demo/commit/abc1234def");
});
