import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Findings, FindingsRun, Round, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, MINUTE, NOW, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("findings.json", SERVE)).json()) as Findings;
const pinnedRun = pinned.runs[0]!;
const pinnedRound = pinnedRun.rounds[0]!;
const pinnedFinding = pinnedRound.findings[0]!;

const flagged = (id: number, ticket: string, facts: Partial<FindingsRun> = {}): FindingsRun => {
  const round: Round = {
    ...pinnedRound,
    round: 1,
    started_ms: NOW - 30 * MINUTE,
    ended_ms: NOW - 30 * MINUTE,
    verdict: "changes_requested",
    findings: [{ ...pinnedFinding, path: `${ticket}.py`, message: `${ticket} needs care` }],
  };
  return { ...pinnedRun, run: id, ticket, phase: "working", outcome: null, rounds: [round], ledger: [], advisories: [], ...facts };
};

/** `demo` on DEMO and `writer` on WRITER, each answering `/findings` with its runs. */
function fleet(demoRuns: FindingsRun[], writerRuns: FindingsRun[]): Fetch {
  const daemon = (project: string) => ({
    status: { ...working, project, runs: [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo"), [WRITER]: daemon("/srv/dev/writer") });
  return async (url) => {
    for (const [base, runs] of [[DEMO, demoRuns], [WRITER, writerRuns]] as const) {
      if (url.startsWith(`${base}/findings?`)) return Response.json({ ...pinned, since: 0, runs, follow_ups: [] });
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

const findingsLink = () => screen.getByRole("navigation", { name: "Pages" }).querySelector('a[href*="#/findings"]')!;
const findingsBadge = () => findingsLink().querySelector("[data-badge]");

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the Findings entry counts the switcher's open findings, not settled ones, in its badge and its name", async () => {
  const demoRuns = [flagged(7, "DEMO-7"), flagged(11, "DEMO-11", { phase: "failed", outcome: "failed" }), flagged(5, "DEMO-5", { phase: "done", outcome: "merged" })];
  const writerRuns = [flagged(3, "WRITER-3")];
  await mount("#/", fleet(demoRuns, writerRuns));
  expect(findingsLink().getAttribute("aria-label")).toBe("Findings, 3 open");
  expect(findingsBadge()!.textContent).toBe("3");
  cleanup();

  await mount("#/?project=writer", fleet(demoRuns, writerRuns));
  expect(findingsLink().getAttribute("aria-label")).toBe("Findings, 1 open");
  expect(findingsBadge()!.textContent).toBe("1");
});

test("with no open findings, only a settled one, the Findings entry has no badge and is named Findings", async () => {
  await mount("#/", fleet([flagged(5, "DEMO-5", { phase: "done", outcome: "merged" })], []));
  expect(findingsLink().getAttribute("aria-label")).toBe("Findings");
  expect(findingsBadge()).toBeNull();
});

test("on the folded rail the Findings entry shows the corner count and keeps its counted name", async () => {
  localStorage.setItem("holophyte.next.rail", "folded");
  await mount("#/", fleet([flagged(7, "DEMO-7"), flagged(11, "DEMO-11")], []));
  expect(findingsLink().getAttribute("aria-label")).toBe("Findings, 2 open");
  expect(findingsLink().textContent).toBe("2");
});
