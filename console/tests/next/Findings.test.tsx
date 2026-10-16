import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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

const AT = NOW - 60 * MINUTE;

const finding = (path: string, severity: string) => ({ ...pinnedFinding, path, line: 3, severity, message: `${path} needs care` });

const round = (n: number, findings: Round["findings"] = []): Round => ({
  ...pinnedRound,
  round: n,
  started_ms: AT + n * MINUTE,
  ended_ms: AT + n * MINUTE,
  verdict: findings.length === 0 ? "pass" : "changes_requested",
  findings,
});

const runOf = (id: number, ticket: string, rounds: Round[], facts: Partial<FindingsRun> = {}): FindingsRun => ({
  ...pinnedRun,
  run: id,
  ticket,
  phase: "working",
  outcome: null,
  rounds,
  ledger: [],
  advisories: [],
  ...facts,
});

const followUp = (ticket: string, facts: Partial<FollowUpRow> = {}): FollowUpRow => ({ ...pinnedFollowUp, ticket, ...facts });

interface Daemon {
  runs?: FindingsRun[];
  followUps?: FollowUpRow[];
  live?: Status["runs"];
  /** `/findings` answers 404, as a daemon without the route does. */
  missing?: boolean;
}

/** `demo` on DEMO and `writer` on WRITER; every url asked is kept. */
function fleet(demo: Daemon, writer: Daemon) {
  const asked: string[] = [];
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: body.live ?? [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo", demo), [WRITER]: daemon("/srv/dev/writer", writer) });
  const fetch: Fetch = async (url) => {
    asked.push(url);
    for (const [base, body] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (!url.startsWith(`${base}/findings?`)) continue;
      if (body.missing) return new Response("not found", { status: 404 });
      return Response.json({ ...pinned, since: 0, runs: body.runs ?? [], follow_ups: body.followUps ?? [] });
    }
    return polls(url);
  };
  return { fetch, asked };
}

const demo7 = runOf(7, "DEMO-7", [round(1, [finding("app.py", "p1")])]);
const demo11 = runOf(11, "DEMO-11", [round(1, [finding("cli.py", "p2")])], { phase: "failed", outcome: "failed" });
const demo5 = runOf(5, "DEMO-5", [round(1, [finding("db.py", "p2")]), round(2)], { phase: "done", outcome: "merged" });
const demo10 = runOf(10, "DEMO-10", [round(1)]);
const live7 = [run(7, "DEMO-7")];
const writerDaemon: Daemon = { followUps: [followUp("WRITER-3", { kind: "feature", state: "pending" })] };
const demoDaemon = (runs: FindingsRun[]): Daemon => ({ runs, live: live7 });

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

const GROUPS = ["Open", "Follow-ups", "Settled this week", "Clean reviews"];
const group = (label: string) => screen.getByRole("region", { name: label });
const counts = () => GROUPS.map((label) => group(label).querySelector("[data-count]")?.textContent ?? null);
const verdict = () => document.querySelector("[data-verdict]")?.textContent;
const countLine = () => document.querySelector("[data-count-line]")?.textContent;
const emptyOf = (label: string) => group(label).querySelector("p")?.textContent ?? null;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => sessionStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("every project's week sorts into the four groups, counted in two digits, under the count line", async () => {
  await mount("#/findings", fleet(demoDaemon([demo7, demo11, demo5, demo10]), writerDaemon).fetch);
  expect(screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.firstChild?.textContent)).toEqual(GROUPS);
  expect(counts()).toEqual(["02", "01", "01", "02"]);
  expect(countLine()).toBe("last 7 days / 5 reviews / 3 findings / worst first");
  expect(document.querySelector("[data-scope-line]")).toBeNull();
});

test("the verdict says how many findings are open, who is on them and why nobody is, then the pending drafts", async () => {
  const drafts = " One follow-up becomes a Backlog draft when its PR merges.";
  await mount("#/findings", fleet(demoDaemon([demo7, demo11, demo5, demo10]), writerDaemon).fetch);
  expect(verdict()).toBe(`Two findings are still open, and one is already being fixed.${drafts}`);
  expect(document.querySelector("[data-verdict] [data-state]")!.getAttribute("data-state")).toBe("needs_you");
  cleanup();

  await mount("#/findings", fleet(demoDaemon([demo7, demo5, demo10]), writerDaemon).fetch);
  expect(verdict()).toBe(`One finding is open and already being fixed.${drafts}`);
  expect(document.querySelector("[data-verdict] [data-state]")!.getAttribute("data-state")).toBe("running");
  cleanup();

  await mount("#/findings", fleet(demoDaemon([demo11, demo5, demo10]), writerDaemon).fetch);
  expect(verdict()).toStartWith("One finding is open and nobody is fixing it: DEMO-11 has failed.");
});

test("a picked project scopes every group and the scope line, whose link clears it", async () => {
  sessionStorage.setItem("holophyte.next.project", "writer");
  await mount("#/findings?project=writer", fleet(demoDaemon([demo7, demo11, demo5, demo10]), writerDaemon).fetch);
  expect(emptyOf("Open")).toBe("Nothing open in writer.");
  expect(counts()).toEqual([null, "01", null, null]);
  expect(document.querySelector("[data-scope-line] span")!.textContent).toBe("Showing writer only");
  const all = screen.getByRole("link", { name: "Show all projects" });
  expect(all.getAttribute("href")).toBe("/next.html#/findings");
  fireEvent.click(all);
  for (let step = 0; step < 2; step += 1) await act(settle);
  expect(window.location.hash).toBe("#/findings");
  expect(sessionStorage.getItem("holophyte.next.project")).toBeNull();
  expect(counts()).toEqual(["02", "01", "01", "02"]);
});

test("a finding adjudicated FOLLOW_UP is counted once, as its follow-up row", async () => {
  const ledger = [{ ...pinnedRun.ledger[0]!, text: "Round 1: REQUEST_CHANGES -> fix round\nImplementer response:\nFOLLOW_UP(feature): validate at the edge @ app.py:3" }];
  const deferred = runOf(8, "DEMO-8", [round(1, [finding("app.py", "p2")])], { ledger });
  const row = followUp("DEMO-8", { run: 8, kind: "feature", state: "pending" });
  await mount("#/findings", fleet({ runs: [deferred], followUps: [row] }, {}).fetch);
  expect(counts()).toEqual([null, "01", null, null]);
});

test("a clean week reads as no open findings, and a daemon without the route leaves the other's groups", async () => {
  await mount("#/findings", fleet({ runs: [runOf(1, "DEMO-1", [round(1)]), runOf(2, "DEMO-2", [round(1)]), runOf(3, "DEMO-3", [round(1)])] }, { runs: [runOf(4, "WRITER-4", [round(1)]), runOf(5, "WRITER-5", [round(1)])] }).fetch);
  expect(verdict()).toBe("No open findings. Five reviews this week raised no findings.");
  expect(document.querySelector("[data-verdict] [data-state]")!.getAttribute("data-state")).toBe("ready_to_merge");
  expect(GROUPS.slice(0, 3).map(emptyOf)).toEqual(["Nothing open.", "No follow-ups.", "Nothing settled this week."]);
  cleanup();

  await mount("#/findings", fleet(demoDaemon([demo7, demo11, demo5, demo10]), { missing: true }).fetch);
  expect(counts()).toEqual(["02", null, "01", "02"]);
});

test("each host is asked for its findings since local midnight six days before today", async () => {
  const { fetch, asked } = fleet({}, {});
  await mount("#/findings", fetch);
  const since = new Date(2026, 9, 9).getTime();
  for (const base of [DEMO, WRITER]) expect(asked).toContain(`${base}/findings?since=${since}`);
});
