import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Findings, FindingsRun, FollowUpRow, Round, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, MINUTE, NOW, run, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("findings.json", SERVE)).json()) as Findings;
const pinnedRun = pinned.runs[0]!;
const pinnedRound = pinnedRun.rounds[0]!;
const pinnedFinding = pinnedRound.findings[0]!;
const pinnedFollowUp = pinned.follow_ups[0]!;
const pinnedLedger = pinnedRun.ledger[0]!;

const AT = NOW - 60 * MINUTE;

const finding = (title: string, severity: string, facts: Partial<Round["findings"][number]> = {}) => ({
  ...pinnedFinding,
  path: `holophyte/${title.toLowerCase().replaceAll(" ", "_")}.py`,
  line: 3,
  severity,
  criterion: null,
  message: `**${title}** The detail of it.`,
  ...facts,
});

const round = (n: number, findings: Round["findings"] = [], reviewer = "reviewer", at = AT + n * MINUTE): Round => ({
  ...pinnedRound,
  round: n,
  started_ms: at,
  ended_ms: at,
  verdict: findings.length === 0 ? "pass" : "changes_requested",
  reviewer_model: reviewer,
  findings,
});

const runOf = (id: number, ticket: string, rounds: Round[], facts: Partial<FindingsRun> = {}): FindingsRun => ({
  ...pinnedRun,
  run: id,
  ticket,
  phase: "working",
  outcome: null,
  pr_url: null,
  rounds,
  ledger: [],
  advisories: [],
  ...facts,
});

const followUp = (id: number, ticket: string, text: string, facts: Partial<FollowUpRow>): FollowUpRow => ({
  ...pinnedFollowUp,
  id,
  ticket,
  text,
  path: null,
  line: null,
  ...facts,
});

interface Daemon {
  runs?: FindingsRun[];
  followUps?: FollowUpRow[];
  live?: Status["runs"];
}

function fleet(demo: Daemon, writer: Daemon = {}): Fetch {
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: body.live ?? [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo", demo), [WRITER]: daemon("/srv/dev/writer", writer) });
  return async (url) => {
    for (const [base, body] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (url.startsWith(`${base}/findings?`)) return Response.json({ ...pinned, since: 0, runs: body.runs ?? [], follow_ups: body.followUps ?? [] });
    }
    return polls(url);
  };
}

async function mount(hash: string, fetchImpl: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let step = 0; step < 4; step += 1) await act(settle);
}

const group = (label: string) => screen.getByRole("region", { name: label });
const rowsOf = (label: string) => [...group(label).querySelectorAll<HTMLElement>("[data-finding-row]")];
const toggle = (row: HTMLElement) => row.querySelector<HTMLButtonElement>("button[aria-expanded]")!;
const titles = (label: string) => rowsOf(label).map((row) => row.querySelector("[data-title]")!.textContent);
const fateOf = (row: HTMLElement) => row.querySelector<HTMLElement>("[data-fate]")!;
const iconOf = (row: HTMLElement) => toggle(row).querySelector<HTMLElement>("[data-state]")!;

function unfoldAll(label: string): HTMLElement[] {
  const rows = rowsOf(label);
  for (const row of rows) fireEvent.click(toggle(row));
  return rows;
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => sessionStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

const lease = finding("Lease never expires", "p1", { path: "holophyte/serve/server.py", line: 142, criterion: "AC4" });
const demo7 = runOf(7, "DEMO-7", [round(1, [lease])]);
const demo11 = runOf(11, "DEMO-11", [round(1, [finding("Flag is ignored", "p2")])], { phase: "failed", outcome: "failed" });
const writer3 = runOf(3, "WRITER-3", [
  round(1, [finding("Retry loops forever", "p1")], "reviewer", AT - 20 * MINUTE),
  round(2, [finding("Retry loops forever", "p1")], "reviewer", AT - 10 * MINUTE),
]);
const reviewing7 = [{ ...run(7, "DEMO-7"), phase: "reviewing", round: 1, max_rounds: 3, heartbeat_age_ms: 30_000 }];

test("open rows read worst first, then the higher round first even when it ended earlier, each pill coloured by severity and naming its project until one is picked", async () => {
  await mount("#/findings", fleet({ runs: [demo11, demo7], live: reviewing7 }, { runs: [writer3] }));
  expect(titles("Open")).toEqual(["Retry loops forever", "Lease never expires", "Flag is ignored"]);
  const pills = rowsOf("Open").map((row) => row.querySelector("[data-pill]")!);
  expect(pills.map((pill) => pill.textContent)).toEqual(["P1", "P1", "P2"]);
  expect(pills[0]!.classList.contains("bg-p1")).toBe(true);
  expect(pills[2]!.classList.contains("bg-p2")).toBe(true);
  expect(rowsOf("Open").map((row) => row.querySelector("[data-project]")?.textContent)).toEqual(["writer", "demo", "demo"]);
  cleanup();

  sessionStorage.setItem("holophyte.next.project", "demo");
  await mount("#/findings?project=demo", fleet({ runs: [demo11, demo7], live: reviewing7 }, { runs: [writer3] }));
  expect(titles("Open")).toEqual(["Lease never expires", "Flag is ignored"]);
  expect(group("Open").querySelector("[data-project]")).toBeNull();
});

test("a row folds open from its tag, title and path to its place and a link to its run", async () => {
  await mount("#/findings", fleet({ runs: [demo7], live: reviewing7 }));
  const [row] = rowsOf("Open");
  expect(row!.querySelector("[data-tag]")!.textContent).toBe("AC4");
  expect(row!.querySelector("[data-title]")!.textContent).toBe("Lease never expires");
  expect(row!.querySelector("[data-path]")!.textContent).toBe("serve/server.py");
  const button = toggle(row!);
  const body = document.getElementById(button.getAttribute("aria-controls")!)!;
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(body.hidden).toBe(true);

  fireEvent.click(button);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(body.hidden).toBe(false);
  expect(body.querySelector("[data-place]")!.textContent).toBe("holophyte/serve/server.py:142 · DEMO-7 · round 1");
  expect(within(body).getByRole("link", { name: "Open run" }).getAttribute("href")).toBe(`/?classic#run=7&daemon=${encodeURIComponent(DEMO)}`);
});

test("an open row's fate says who is fixing it, or why nobody is", async () => {
  await mount("#/findings", fleet({ runs: [demo7, demo11], live: reviewing7 }));
  const [fixing, idle] = unfoldAll("Open");
  expect(fateOf(fixing!).textContent).toBe("being fixed · reviewing, round 2 of 3");
  expect(iconOf(fixing!).getAttribute("data-state")).toBe("running");
  expect(fateOf(idle!).textContent).toBe("open · the run failed, so nobody is fixing it");
  expect(iconOf(idle!).getAttribute("data-state")).toBe("needs_you");
  expect(fateOf(idle!).classList.contains("text-ask")).toBe(true);
});

test("settled rows say what became of each finding, under a quiet icon", async () => {
  const declineLedger = [{
    ...pinnedLedger,
    text: "Round 1: REQUEST_CHANGES -> fix round\nImplementer response:\nDECLINE holophyte/serve/views.py:20 — the store column is lease_age_ms",
  }];
  const fixed = runOf(5, "DEMO-5", [round(1, [finding("Export drops a column", "p1")]), round(2)], { phase: "done", outcome: "merged" });
  const declined = runOf(6, "DEMO-6", [round(1, [finding("Rename lease_age_ms", "p2", { path: "holophyte/serve/views.py", line: 20 })])], { ledger: declineLedger });
  const mergedOpen = runOf(8, "DEMO-8", [round(1, [finding("Log line is noisy", "nit")])], { phase: "done", outcome: "merged" });
  await mount("#/findings", fleet({ runs: [mergedOpen, declined, fixed] }));
  const rows = unfoldAll("Settled this week");
  expect(rows.map((row) => fateOf(row).textContent)).toEqual([
    "fixed in round 2",
    "declined · the store column is lease_age_ms",
    "merged with this finding open",
  ]);
  for (const row of rows) expect(iconOf(row).classList.contains("text-ink-3")).toBe(true);
});

test("follow-ups read their state's fate, the pending ones first", async () => {
  const shipping = runOf(7, "DEMO-7", [round(1)], { pr_url: "https://github.com/example/demo/pull/521" });
  const advised = runOf(10, "DEMO-10", [round(1)], { advisories: [{ tone: "advisory", message: "Consider a smaller diff." }] });
  const rows = [
    followUp(1, "DEMO-5", "Fail the export test on a renamed column.", { run: 5, kind: "guardrail", state: "ledger", created_ms: AT + 20 * MINUTE }),
    followUp(2, "DEMO-7", "Guard the lease clock.", { run: 7, kind: "guardrail", state: "pending", created_ms: AT - 20 * MINUTE }),
    followUp(3, "DEMO-4", "Name each worker's route.", { run: 4, kind: "feature", state: "filed", filed_as: "HOLO-248", created_ms: AT + 30 * MINUTE }),
    followUp(4, "DEMO-7", "Show the diff size.", { run: 7, kind: "feature", state: "pending", created_ms: AT - 10 * MINUTE }),
    followUp(5, "DEMO-3", "Page the ledger.", { run: 3, kind: "feature", state: "unfiled", error: "board refused", created_ms: AT + 10 * MINUTE }),
  ];
  await mount("#/findings", fleet({ runs: [shipping, advised], followUps: rows }));
  const unfolded = unfoldAll("Follow-ups");
  expect(unfolded.map((row) => fateOf(row).textContent)).toEqual([
    "becomes a Backlog draft when PR #521 merges",
    "joins the findings ledger when DEMO-7 merges",
    "filed as HOLO-248 · Backlog",
    "added to the findings ledger when DEMO-5 merged",
    "not filed: board refused",
    "noted, not acted on",
  ]);
  expect(fateOf(unfolded[4]!).classList.contains("text-bad")).toBe(true);
});

test("a clean review reads as one sentence and its reviewer", async () => {
  await mount("#/findings", fleet({ runs: [runOf(10, "DEMO-10", [round(1, [], "reviewer-model")])] }));
  const [line] = group("Clean reviews").querySelectorAll("[data-clean]");
  expect([...line!.querySelectorAll("p")].map((p) => p.textContent)).toEqual([
    "DEMO-10 passed round 1 with no findings.",
    "reviewed by reviewer-model",
  ]);
});
