import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Findings, FindingsRun, FollowUpRow, Round, Status, TicketDefect } from "../../src/lib/contract.gen";
import { formatStamp } from "../../src/lib/format";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { dayRangeLabel } from "../../src/app/components/ListFilters";
import { weekSince } from "../../src/app/queries";
import { ConsoleApp } from "../../src/app/router";
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
  defects?: TicketDefect[];
  /** `/findings` answers 404, as a daemon without the route does. */
  missing?: boolean;
}

/** `demo` on DEMO and `writer` on WRITER, or the projects `names`; every url asked is kept. */
function fleet(demo: Daemon, writer: Daemon, names = ["demo", "writer"]) {
  const asked: string[] = [];
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: body.live ?? [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon(`/srv/dev/${names[0]}`, demo), [WRITER]: daemon(`/srv/dev/${names[1]}`, writer) });
  const fetch: Fetch = async (url) => {
    asked.push(url);
    for (const [base, body] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (!url.startsWith(`${base}/findings?`)) continue;
      if (body.missing) return new Response("not found", { status: 404 });
      return Response.json({ ...pinned, since: 0, runs: body.runs ?? [], follow_ups: body.followUps ?? [], ticket_defects: body.defects ?? [] });
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

async function mount(hash: string, fetchImpl: Fetch, now = NOW) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = now;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
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

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
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
  expect(all.getAttribute("href")).toBe("/#/findings");
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

const pressed = () => within(screen.getByRole("group", { name: "Range" })).getAllByRole("button", { pressed: true }).map((button) => button.textContent);
const findingsAsked = (asked: string[]) => asked.filter((url) => url.includes("/findings?"));

async function choose(...names: string[]) {
  fireEvent.click(screen.getByRole("button", { name: "Filter" }));
  const dialog = screen.getByRole("dialog", { name: "Filter findings" });
  for (const name of names) {
    fireEvent.click(within(dialog).getByRole("checkbox", { name }));
    for (let step = 0; step < 2; step += 1) await act(settle);
  }
}

test("with no filter, 7d is pressed and each host is asked since local midnight six days before today, with no until", async () => {
  const { fetch, asked } = fleet({}, {});
  await mount("#/findings", fetch);
  expect(pressed()).toEqual(["7d"]);
  const since = new Date(2026, 9, 9).getTime();
  for (const base of [DEMO, WRITER]) expect(asked).toContain(`${base}/findings?since=${since}&limit=500`);
  expect(findingsAsked(asked).filter((url) => url.includes("until="))).toEqual([]);
});

test("where today's midnight falls in a clock change, the default window still starts at weekSince, shared with the badge", async () => {
  const zone = process.env.TZ;
  process.env.TZ = "America/Santiago";
  try {
    const now = new Date(2026, 8, 6, 12).getTime();
    const { fetch, asked } = fleet({}, {});
    await mount("#/findings", fetch, now);
    expect([...new Set(findingsAsked(asked).map((url) => url.slice(url.indexOf("/findings"))))]).toEqual([`/findings?since=${weekSince(now)}&limit=500`]);
  } finally {
    process.env.TZ = zone;
  }
});

test("range 30d asks since local midnight 29 days before today and names the last 30 days in the count, verdict and empty lines", async () => {
  const { fetch, asked } = fleet({}, {});
  await mount("#/findings?range=30d", fetch);
  expect(pressed()).toEqual(["30d"]);
  expect(asked).toContain(`${DEMO}/findings?since=${new Date(2026, 8, 16).getTime()}&limit=500`);
  expect(countLine()).toStartWith("last 30 days / ");
  expect(verdict()).toBe("No reviews in the last 30 days.");
  const settled = "Settled in the last 30 days";
  expect([settled, "Clean reviews"].map(emptyOf)).toEqual(["Nothing settled in the last 30 days.", "No clean reviews in the last 30 days."]);
});

test("a custom range from 1 to 15 October asks since midnight 1 October until midnight 16 October and heads the count line with its label", async () => {
  const { fetch, asked } = fleet({}, {});
  await mount("#/findings?from=2026-10-01&to=2026-10-15", fetch);
  const since = new Date(2026, 9, 1).getTime();
  const until = new Date(2026, 9, 16).getTime();
  expect(asked).toContain(`${DEMO}/findings?since=${since}&limit=500&until=${until}`);
  expect(countLine()).toStartWith(`${dayRangeLabel("2026-10-01", "2026-10-15")} / `);
});

const openTitles = () => [...group("Open").querySelectorAll("[data-finding-row] [data-title]")].map((title) => title.textContent);

test("choosing severities must and should leaves only those open rows, counted 02", async () => {
  const runs = [
    runOf(1, "DEMO-1", [round(1, [finding("must.py", "p1")])]),
    runOf(2, "DEMO-2", [round(1, [finding("should.py", "p2")])]),
    runOf(3, "DEMO-3", [round(1, [finding("nit.py", "nit")])]),
  ];
  await mount("#/findings", fleet({ runs, live: live7 }, {}).fetch);
  expect(openTitles()).toHaveLength(3);
  await choose("must", "should");
  expect(openTitles()).toEqual(["must.py needs care", "should.py needs care"]);
  expect(group("Open").querySelector("[data-count]")!.textContent).toBe("02");
});

test("choosing status Fixed alone leaves Settled with only the fixed row and every other group empty", async () => {
  const ledger = [{ ...pinnedRun.ledger[0]!, text: "Round 1: REQUEST_CHANGES -> fix round\nImplementer response:\nDECLINE cli.py:3 — the caller validates it" }];
  const declined = runOf(12, "DEMO-12", [round(1, [finding("cli.py", "p2")])], { ledger });
  await mount("#/findings", fleet(demoDaemon([demo7, demo5, declined]), writerDaemon).fetch);
  expect(emptyOf("Open")).toBeNull();
  const settled = () => [...group("Settled this week").querySelectorAll("[data-finding-row] [data-title]")].map((title) => title.textContent);
  expect(settled()).toEqual(["db.py needs care", "cli.py needs care"]);
  await choose("Fixed");
  expect(settled()).toEqual(["db.py needs care"]);
  expect(["Open", "Follow-ups", "Clean reviews"].map(emptyOf)).toEqual(["Nothing open.", "No follow-ups.", "No clean reviews this week."]);
});

test("a linked range and severity press 30d, show only must rows under a Severity chip, and clearing the chip drops severity from the URL", async () => {
  const runs = [runOf(1, "DEMO-1", [round(1, [finding("must.py", "p0")])]), runOf(2, "DEMO-2", [round(1, [finding("should.py", "p2")])])];
  await mount("#/findings?range=30d&severity=must", fleet({ runs, live: live7 }, writerDaemon).fetch);
  expect(pressed()).toEqual(["30d"]);
  expect(openTitles()).toEqual(["must.py needs care"]);
  expect(emptyOf("Follow-ups")).toBe("No follow-ups.");
  const chip = screen.getByRole("button", { name: "Remove Severity: must" });
  fireEvent.click(chip);
  for (let step = 0; step < 2; step += 1) await act(settle);
  expect(window.location.hash).toContain("range=30d");
  expect(window.location.hash).not.toContain("severity");
  expect(openTitles()).toEqual(["must.py needs care", "should.py needs care"]);
});

const subheadings = (label: string) => [...group(label).querySelectorAll("h3")].map((heading) => heading.textContent);
const rowsUnder = (label: string) =>
  [...group(label).querySelectorAll("[data-project-group]")].map((part) =>
    [...part.querySelectorAll("li")].map((row) => row.querySelector("[data-title], [data-sentence]")!.textContent),
  );

const alpha = { runs: [runOf(1, "ALPHA-1", [round(1, [finding("a.py", "p1")])]), runOf(2, "ALPHA-2", [round(1, [finding("b.py", "p2")])])] };
const beta = { runs: [runOf(3, "BETA-3", [round(1, [finding("c.py", "p1")])])] };

test("an open finding of run 7, unfolded, links to run 7's Run page", async () => {
  await mount("#/findings", fleet(demoDaemon([demo7]), {}).fetch);
  const row = group("Open").querySelector<HTMLElement>("[data-finding-row]")!;
  const toggle = row.querySelector<HTMLButtonElement>("button[aria-expanded]")!;
  fireEvent.click(toggle);
  const body = document.getElementById(toggle.getAttribute("aria-controls")!)!;
  expect(within(body).getByRole("link", { name: "Open run" }).getAttribute("href")).toEndWith("#/runs/demo/7");
});

test("with every project shown, Open lists its rows under a sub-heading per project, the one with most rows first", async () => {
  await mount("#/findings", fleet(beta, alpha, ["beta", "alpha"]).fetch);
  expect(subheadings("Open")).toEqual(["alpha 2", "beta 1"]);
  expect(rowsUnder("Open")).toEqual([["a.py needs care", "b.py needs care"], ["c.py needs care"]]);
  expect(group("Open").querySelector("[data-project]")).toBeNull();
  expect(emptyOf("Follow-ups")).toBe("No follow-ups.");
  cleanup();

  await mount("#/findings", fleet(alpha, beta, ["beta", "alpha"]).fetch);
  expect(subheadings("Open")).toEqual(["beta 2", "alpha 1"]);
});

test("with a project chosen, the Open group lists its rows with no project sub-heading", async () => {
  sessionStorage.setItem("holophyte.next.project", "alpha");
  await mount("#/findings?project=alpha", fleet(beta, alpha, ["beta", "alpha"]).fetch);
  expect(group("Open").querySelectorAll("[data-finding-row]")).toHaveLength(2);
  expect(subheadings("Open")).toEqual([]);
});

test("clean reviews group under the same per-project sub-headings", async () => {
  const clean = (id: number, ticket: string) => runOf(id, ticket, [round(1)]);
  await mount("#/findings", fleet({ runs: [clean(1, "ALPHA-1"), clean(2, "ALPHA-2")] }, { runs: [clean(3, "BETA-3")] }, ["alpha", "beta"]).fetch);
  expect(subheadings("Clean reviews")).toEqual(["alpha 2", "beta 1"]);
  expect(rowsUnder("Clean reviews")).toEqual([
    ["ALPHA-1 passed round 1 with no findings.", "ALPHA-2 passed round 1 with no findings."],
    ["BETA-3 passed round 1 with no findings."],
  ]);
});

const REVISED = NOW - 30 * MINUTE;

const defect = (ticket: string, run: number, facts: Partial<TicketDefect> = {}): TicketDefect => ({
  ticket,
  run,
  kind: "contradiction",
  criteria: ["C4"],
  reason: "the criterion contradicts the verify",
  at: AT,
  revision: 2,
  revised_ms: REVISED,
  requeue_note: null,
  ...facts,
});

const defectRows = (scope: ParentNode = group("Ticket defects")) => [...scope.querySelectorAll("[data-defect-row]")];
const defectTickets = (scope: ParentNode) => defectRows(scope).map((row) => row.querySelector("a")!.textContent);
const moreOf = (row: Element) => row.querySelector("button[aria-controls]");

test("a ticket defect reads its ticket, criterion, stop, revision with its time and note, and links its ticket page", async () => {
  const note = "C4 now names the export's columns";
  await mount("#/findings", fleet({ defects: [defect("DEMO-7", 7, { requeue_note: note })] }, {}).fetch);
  const [row, ...rest] = defectRows();
  expect(rest).toHaveLength(0);
  const text = row!.textContent!;
  for (const part of ["DEMO-7", "C4", "contradiction", "revision 2", formatStamp(REVISED), note]) expect(text).toContain(part);
  expect(screen.getByRole("link", { name: "DEMO-7" }).getAttribute("href")).toEndWith("#/tickets/demo/DEMO-7");
});

test("a stuck criterion shows its criterion tag and a stuck review shows none", async () => {
  const stuck = [
    defect("DEMO-2", 2, { kind: "criterion_stuck", criteria: ["C2"], at: AT + 2 * MINUTE }),
    defect("DEMO-3", 3, { kind: "review_stuck", criteria: [], at: AT + MINUTE }),
  ];
  await mount("#/findings", fleet({ defects: stuck }, {}).fetch);
  const [first, second] = defectRows();
  expect(first!.querySelector("[data-stop]")!.textContent).toBe("stuck criterion");
  expect([...first!.querySelectorAll("[data-tag]")].map((tag) => tag.textContent)).toEqual(["C2"]);
  expect(second!.querySelector("[data-stop]")!.textContent).toBe("stuck review");
  expect(second!.querySelectorAll("[data-tag]")).toHaveLength(0);
});

test("ticket defects group by project, newest stop first, and a chosen project lists only its own", async () => {
  const demo = { defects: [defect("DEMO-1", 1, { at: AT }), defect("DEMO-2", 2, { at: AT + 5 * MINUTE })] };
  const writer = { defects: [defect("WRITER-4", 4, { at: AT + 2 * MINUTE })] };
  await mount("#/findings", fleet(demo, writer).fetch);
  expect(subheadings("Ticket defects")).toEqual(["demo 2", "writer 1"]);
  const parts = [...group("Ticket defects").querySelectorAll("[data-project-group]")];
  expect(parts.map(defectTickets)).toEqual([["DEMO-2", "DEMO-1"], ["WRITER-4"]]);
  cleanup();

  sessionStorage.setItem("holophyte.next.project", "writer");
  await mount("#/findings?project=writer", fleet(demo, writer).fetch);
  expect(defectTickets(group("Ticket defects"))).toEqual(["WRITER-4"]);
  expect(subheadings("Ticket defects")).toEqual([]);
});

test("a two-line requeue note shows its first line until More opens it, then More reads Less", async () => {
  await mount("#/findings", fleet({ defects: [defect("DEMO-7", 7, { requeue_note: "C4 now names the columns\nand drops the count" })] }, {}).fetch);
  const [row] = defectRows();
  expect(row!.textContent).toContain("C4 now names the columns");
  expect(row!.textContent).not.toContain("and drops the count");
  fireEvent.click(moreOf(row!)!);
  expect(row!.textContent).toContain("and drops the count");
  expect(moreOf(row!)!.textContent).toBe("Less");
});

test("a short requeue note has no More, and a missing note draws no note", async () => {
  const defects = [defect("DEMO-7", 7, { requeue_note: "C4 names the columns", at: AT + MINUTE }), defect("DEMO-8", 8, { requeue_note: null })];
  await mount("#/findings", fleet({ defects }, {}).fetch);
  const [short, none] = defectRows();
  expect(short!.textContent).toContain("C4 names the columns");
  expect(moreOf(short!)).toBeNull();
  expect(none!.querySelector("[data-note]")).toBeNull();
  expect(moreOf(none!)).toBeNull();
});

test("with no ticket defects the page has no Ticket defects group and keeps its other groups", async () => {
  await mount("#/findings", fleet(demoDaemon([demo7, demo11, demo5, demo10]), writerDaemon).fetch);
  expect(screen.queryByRole("region", { name: "Ticket defects" })).toBeNull();
  expect(document.body.textContent!.toLowerCase()).not.toContain("defect");
  for (const label of GROUPS) expect(group(label)).toBeTruthy();
});
