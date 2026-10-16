import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunAsks, RunDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const [servedRound] = servedRun.rounds;
const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();

const finding = { ...servedRound!.findings[0]!, severity: "p1", message: "the dry run writes a row", path: "holophyte/serve/server.py", line: 142 };
const round1 = { ...servedRound!, round: 1, started_ms: at(16, 44), ended_ms: at(16, 45), verdict: "changes_requested", findings: [finding] };
const note = { id: 1, author: "operator", run: 12, consumed_by: null, consumed_ms: null, interrupt: false, withdrawn: false };

const liveDetail: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 12, ticket: "DEMO-12", title: "Sweep the leases", phase: "reviewing", started_ms: at(16, 36) },
  events: [
    { at: at(16, 36), kind: "phase_change", summary: "claimed -> working" },
    { at: at(16, 45), kind: "phase_change", summary: "reviewing -> addressing" },
  ],
  rounds: [round1],
  steer_notes: [
    { ...note, kind: "amendment", note: "don't add a new flag; reuse --act", at: at(16, 40), consumed_by: 12, consumed_ms: at(16, 41) },
    { ...note, id: 2, kind: "hint", note: "the test lives in test_sweep", at: at(16, 50) },
  ],
};

const asks: RunAsks = {
  run: 12,
  ticket: "DEMO-12",
  pr_url: null,
  asks: [{ id: 3, question: "does the dry run print anything?", author: "operator", asked_ms: at(16, 47), answered_ms: at(16, 48), url: null, answer: "One line per lease.\nMore" }],
};

/** DEMO live in run 12; `/runs/12` and `/runs/12/asks` answer `bodies`, read at each request. */
function progressFetch(bodies: { detail: RunDetail; asks: RunAsks }): Fetch {
  const polls = fleet({ items: [], runs: [{ ...run(12, "DEMO-12"), phase: "reviewing", round: 1, max_rounds: 3 }] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/runs/12`) return Response.json(bodies.detail);
    if (url === `${DEMO}/runs/12/asks`) return Response.json(bodies.asks);
    return polls(url);
  };
}

async function mount(fetch: Fetch) {
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return queries;
}

const section = () => screen.getByRole("region", { name: "Progress" });
const items = () => Array.from(section().querySelectorAll("li"));
const line = (li: Element) => ({
  time: li.querySelector("time")!.textContent,
  kind: li.getAttribute("data-kind"),
  text: li.querySelector("[data-line-text]")!.textContent,
  sub: Array.from(li.querySelectorAll("[data-sub]"), (sub) => sub.textContent),
});

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

test("a changes-requested round lists its must-fix finding and is followed by the loop back to implement", async () => {
  await mount(progressFetch({ detail: { ...liveDetail, steer_notes: [] }, asks: { ...asks, asks: [] } }));
  expect(items().map(line)).toEqual([
    { time: "16:36", kind: "event", text: "Run started · implementing", sub: [] },
    {
      time: "16:45",
      kind: "round",
      text: "Review round 1: changes requested, 1 must-fix",
      sub: ["P1 · the dry run writes a row · holophyte/serve/server.py:142"],
    },
    { time: "16:45", kind: "loop", text: "Back to implement", sub: [] },
  ]);
});

test("steers and asks sit in time order among the events, each with what came of it", async () => {
  await mount(progressFetch({ detail: liveDetail, asks }));
  expect(items().map((li) => `${line(li).time} ${line(li).text}${line(li).sub.map((sub) => ` | ${sub}`).join("")}`)).toEqual([
    "16:36 Run started · implementing",
    "16:40 You steered: don't add a new flag; reuse --act | → the agent read it at 16:41",
    "16:45 Review round 1: changes requested, 1 must-fix | P1 · the dry run writes a row · holophyte/serve/server.py:142",
    "16:45 Back to implement",
    "16:47 You asked: does the dry run print anything? | → answered: One line per lease.",
    "16:50 You hinted: the test lives in test_sweep | → waiting until the agent reads it",
  ]);
});

test("a live run's label says live and only a line a later poll adds is marked new", async () => {
  const bodies = { detail: liveDetail, asks };
  const queries = await mount(progressFetch(bodies));
  expect(section().querySelector("h2")!.textContent).toBe("Progresslive");
  expect(section().querySelectorAll("[data-new]")).toHaveLength(0);
  bodies.detail = { ...liveDetail, events: [...liveDetail.events, { at: at(16, 52), kind: "phase_change", summary: "addressing -> verifying" }] };
  await act(async () => {
    await queries.refetchQueries();
  });
  for (let round = 0; round < 3; round += 1) await act(settle);
  const fresh = Array.from(section().querySelectorAll("[data-new]"), (li) => li.querySelector("[data-line-text]")!.textContent);
  expect(fresh).toEqual(["Fix applied · verifying"]);
});

test("an ended run shows no live mark and its last line names its outcome, past a note steered after it ended", async () => {
  const failed: RunDetail = {
    ...liveDetail,
    run: { ...liveDetail.run, phase: "failed", outcome: "failed", failure_kind: "verify", outcome_reason: "2 tests red\nmore", ended_ms: at(16, 55) },
    steer_notes: [...liveDetail.steer_notes, { ...note, id: 4, kind: "amendment", note: "for the retry: keep --act", at: at(17, 0) }],
  };
  await mount(progressFetch({ detail: failed, asks }));
  expect(section().querySelector("[data-live]")).toBeNull();
  expect(section().querySelector("h2")!.textContent).toBe("Progress");
  expect(line(items().at(-1)!)).toEqual({ time: "16:55", kind: "failure", text: "Failed at verify: 2 tests red", sub: [] });
});

test("a passing round reads passed, and a note withdrawn after the agent read it keeps its line", async () => {
  const passed: RunDetail = {
    ...liveDetail,
    events: [liveDetail.events[0]!],
    rounds: [{ ...round1, verdict: "pass", findings: [] }],
    steer_notes: [
      { ...liveDetail.steer_notes[0]!, withdrawn: true },
      { ...liveDetail.steer_notes[1]!, withdrawn: true },
    ],
  };
  await mount(progressFetch({ detail: passed, asks: { ...asks, asks: [] } }));
  expect(items().map((li) => `${line(li).time} ${line(li).text}${line(li).sub.map((sub) => ` | ${sub}`).join("")}`)).toEqual([
    "16:36 Run started · implementing",
    "16:40 You steered: don't add a new flag; reuse --act | → the agent read it at 16:41",
    "16:45 Review round 1: passed",
  ]);
});

test("the Stage line links to the Progress section by its id", async () => {
  await mount(progressFetch({ detail: liveDetail, asks }));
  const link = screen.getByRole("link", { name: "history in Progress" });
  expect(link.getAttribute("href")).toBe("#progress");
  expect(document.getElementById("progress")).toBe(section());
});
