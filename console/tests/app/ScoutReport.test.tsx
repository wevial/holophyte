import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ScoutDetail, ScoutFinding, ScoutReportPass, Scouts } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const servedPass = served.reports[0]!;
const servedReport = servedPass.report;
const [servedFile, servedCommand] = servedReport.findings[0]!.evidence;

const BASE = "a41c9e2b7d0f4c3e9a1b2c3d4e5f60718293a4b5";
const COMMAND = "pytest tests/test_listener_lock.py -x";
const OUTPUT = "sqlite3.OperationalError: database is locked";
const WHY = "past the end of the file (240 lines)";

const file = (path: string, line: number, verified = true, why: string | null = null) => ({ ...servedFile!, file: path, line, verified, why });
const finding = (title: string, status: ScoutFinding["status"], evidence: ScoutFinding["evidence"] = []): ScoutFinding => ({
  ...servedReport.findings[0]!, title, status, evidence,
});

const bugFindings = [
  finding("The comment listener keeps its write transaction open while it calls publish.", "confirmed", [
    file("lotuspod/listener.py", 132), { ...servedCommand!, command: COMMAND, output: OUTPUT },
  ]),
  finding("Loop claims use the default 5 s busy_timeout.", "confirmed", [file("lotuspod/store.py", 41)]),
  finding("The digest job writes through its own connection.", "partial", [file("lotuspod/digest.py", 57), file("lotuspod/digest.py", 301, false, WHY)]),
  finding("busy_timeout is lost when the store reconnects.", "unconfirmed", [file("lotuspod/store.py", 41)]),
];

const hypotheses = [
  { hypothesis: "The listener holds the lock across publish", separates: "The failing test passes once publish moves after commit.", verdict: "supported" as const },
  { hypothesis: "busy_timeout is lost on reconnect", separates: "A forced reconnect keeps the 5 s timeout.", verdict: "ruled_out" as const },
  { hypothesis: "Host write stalls cause the locks", separates: "Locks happen in the test with no stall injected.", verdict: "open" as const },
];

const pass = (number: number, findings: ScoutFinding[], reproduction: ScoutReportPass["report"]["reproduction"]): ScoutReportPass => ({
  ...servedPass, pass: number, report: { ...servedReport, findings, reproduction },
});

const bug: ScoutDetail = {
  ...served,
  scout: { ...served.scout, id: 31, kind: "bug", phase: "done", base_sha: BASE },
  reports: [pass(1, bugFindings, { level: "test", hypotheses })],
};

const research: ScoutDetail = {
  ...bug,
  scout: { ...bug.scout, id: 32, kind: "research" },
  reports: [pass(1, [finding("Streams resume from their last offset.", "confirmed", [{ url: "https://example.com/notes/streams" }])], null)],
};

const twoPasses: ScoutDetail = {
  ...bug,
  scout: { ...bug.scout, id: 33 },
  reports: [bug.reports[0]!, pass(2, [finding("The digest also holds the lock.", "partial")], { level: "test", hypotheses })],
};

const marked: ScoutDetail = {
  ...bug,
  scout: { ...bug.scout, id: 34 },
  reports: [pass(1, [finding("**Bold** and `code`", "confirmed")], null)],
};

const unreported = (id: number, phase: ScoutDetail["scout"]["phase"]): ScoutDetail => ({
  ...bug, scout: { ...bug.scout, id, phase, ended_ms: phase === "failed" ? bug.scout.ended_ms : null }, reports: [],
});

const scouts: Record<number, ScoutDetail> = { 31: bug, 32: research, 33: twoPasses, 34: marked, 35: unreported(35, "swarming"), 36: unreported(36, "failed") };

function scoutFetch(shown: Record<number, ScoutDetail>): Fetch {
  const polls = fleet({ items: [] }, { items: [] });
  return async (url) => {
    const id = new RegExp(`^${DEMO}/scouts/(\\d+)$`).exec(url)?.[1];
    if (id != null) {
      const detail = shown[Number(id)];
      return detail ? Response.json(detail) : Response.json({ error: "no such scout" }, { status: 404 });
    }
    if (url === `${DEMO}/scouts`) return Response.json({ ...servedScouts, scouts: [] });
    return polls(url);
  };
}

async function mount(id: number, shown = scouts) {
  window.location.hash = `#/scouts/demo/${id}`;
  const fake = fakeDeps(scoutFetch(shown));
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  await screen.findByRole("region", { name: "Progress" });
}

const report = () => screen.getByRole("region", { name: "Findings" });
const count = () => report().querySelector("[data-report-count]")!.textContent;
const reproduction = () => report().querySelector("[data-reproduction]");
const findingRows = () => within(screen.getByRole("list", { name: "Findings" })).getAllByRole("listitem");
const rowId = (row: HTMLElement) => row.querySelector("[data-finding-id]")!.textContent;
const chip = (row: HTMLElement) => row.querySelector("[data-finding-status]")!.textContent;
const openCitations = async (row: HTMLElement) => {
  await act(async () => void fireEvent.click(within(row).getByRole("button", { name: /^\d+ citations?$/ })));
};

beforeAll(() => {
  process.env.TZ = "America/Los_Angeles";
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});
afterAll(() => {
  process.env.TZ = zone;
});

test("a done bug scout's Findings counts its findings, reads its reproduction with its hypotheses tally, and chips each finding's own status", async () => {
  await mount(31);
  expect(count()).toBe("4");
  expect(reproduction()!.textContent).toBe("reproduced in a test · 3 hypotheses · 1 supported · 1 ruled out · 1 open");
  const rows = findingRows();
  expect(rows.map(rowId)).toEqual(["F1", "F2", "F3", "F4"]);
  expect(rows.map(chip)).toEqual(["confirmed", "confirmed", "partial", "unconfirmed"]);
});

test("a verified citation is titled with its base and an unverified one reads its reason as its description", async () => {
  await mount(31);
  const f3 = findingRows()[2]!;
  await openCitations(f3);
  const verified = within(f3).getByText("lotuspod/digest.py:57");
  expect(verified.getAttribute("title")).toBe("at base a41c9e2");
  const unverified = within(f3).getByText("? lotuspod/digest.py:301 unverified");
  expect(within(f3).getByRole("generic", { description: WHY })).toBe(unverified);
});

test("a command chip opens its command and output under the finding and closes again", async () => {
  await mount(31);
  const f1 = findingRows()[0]!;
  await openCitations(f1);
  const chipButton = within(f1).getByRole("button", { name: `${COMMAND} ▸` });
  expect(chipButton.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText(OUTPUT, { exact: false })).toBeNull();

  await act(async () => void fireEvent.click(chipButton));
  expect(chipButton.textContent).toContain("▾");
  expect(chipButton.getAttribute("aria-expanded")).toBe("true");
  const block = document.getElementById(chipButton.getAttribute("aria-controls")!)!;
  expect(block.textContent).toContain(`$ ${COMMAND}`);
  expect(block.textContent).toContain(OUTPUT);

  await act(async () => void fireEvent.click(chipButton));
  expect(chipButton.getAttribute("aria-expanded")).toBe("false");
  expect(document.getElementById(chipButton.getAttribute("aria-controls")!)).toBeNull();
});

test("an opened command closes when the page moves to another scout", async () => {
  const visit = async (id: number) => {
    await act(async () => {
      window.location.hash = `#/scouts/demo/${id}`;
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    for (let round = 0; round < 6; round += 1) await act(settle);
  };
  const commandChip = () => within(findingRows()[0]!).getByRole("button", { name: new RegExp(`^${COMMAND}`) });
  await mount(33);
  await visit(31);
  expect(count()).toBe("4");
  await openCitations(findingRows()[0]!);
  await act(async () => void fireEvent.click(commandChip()));
  expect(commandChip().getAttribute("aria-expanded")).toBe("true");

  await visit(33);
  expect(count()).toBe("5");
  expect(within(findingRows()[0]!).queryByRole("button", { name: new RegExp(`^${COMMAND}`) })).toBeNull();
  await openCitations(findingRows()[0]!);
  expect(commandChip().getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText(OUTPUT, { exact: false })).toBeNull();
});

test("a research scout's Findings links its url evidence without the scheme and has no reproduction or Hypotheses", async () => {
  await mount(32);
  expect(reproduction()).toBeNull();
  await openCitations(findingRows()[0]!);
  const link = within(findingRows()[0]!).getByRole("link", { name: "example.com/notes/streams" });
  expect(link.getAttribute("href")).toBe("https://example.com/notes/streams");
  expect(screen.queryByRole("list", { name: "Hypotheses" })).toBeNull();
});

test("a bug scout's hypotheses read in order with what separates each and their verdicts", async () => {
  await mount(31);
  expect(screen.queryByRole("list", { name: "Hypotheses" })).toBeNull();
  await act(async () => void fireEvent.click(reproduction()!));
  const rows = within(screen.getByRole("list", { name: "Hypotheses" })).getAllByRole("listitem");
  expect(rows.map((row) => row.querySelector("[data-hypothesis-id]")!.textContent)).toEqual(["H1", "H2", "H3"]);
  expect(rows[0]!.textContent).toContain("The listener holds the lock across publish");
  expect(rows[0]!.textContent).toContain("The failing test passes once publish moves after commit.");
  expect(rows.map((row) => row.querySelector("[data-verdict]")!.textContent)).toEqual(["supported", "ruled out", "open"]);
});

test("a second pass's findings continue the numbering, carry a pass tag, and the count takes in every pass", async () => {
  await mount(33);
  const rows = findingRows();
  expect(rows).toHaveLength(5);
  expect(rowId(rows[4]!)).toBe("F5");
  expect(rows[4]!.textContent).toContain("pass 2");
  expect(rows[0]!.textContent).not.toContain("pass ");
  expect(count()).toBe("5");
});

test("a swarming or failed scout with no report has no Findings region", async () => {
  await mount(35);
  expect(screen.queryByRole("region", { name: "Findings" })).toBeNull();
  cleanup();
  await mount(36);
  expect(screen.queryByRole("region", { name: "Findings" })).toBeNull();
});

test("a finding title reads as plain text, never markdown", async () => {
  await mount(34);
  const [row] = findingRows();
  expect(row!.querySelector("[data-finding-title]")!.textContent).toBe("**Bold** and `code`");
  expect(row!.querySelector("strong, code")).toBeNull();
});

test("a text evidence item reads as plain text with no link or button, beside file and url items that read as before", async () => {
  const text = "No KillMode in deploy/*.service; holophyte-loop@.service:13-17 sets Type=simple";
  const mixed: ScoutDetail = {
    ...bug,
    reports: [pass(1, [
      finding("Helpers outlive the loop.", "unconfirmed", [{ text }]),
      finding("The unit sets no KillMode.", "confirmed", [file("deploy/holophyte-loop@.service", 13), { url: "https://example.com/systemd/kill" }]),
    ], null)],
  };
  await mount(31, { 31: mixed });
  const [f1, f2] = findingRows();
  await openCitations(f1!);
  await openCitations(f2!);
  const evidence = f1!.querySelector("[data-evidence]") as HTMLElement;
  expect(evidence.textContent).toBe(text);
  expect(within(evidence).queryByRole("link")).toBeNull();
  expect(within(evidence).queryByRole("button")).toBeNull();
  expect(within(f2!).getByText("deploy/holophyte-loop@.service:13")).toBeTruthy();
  expect(within(f2!).getByRole("link", { name: "example.com/systemd/kill" }).getAttribute("href")).toBe("https://example.com/systemd/kill");
});
