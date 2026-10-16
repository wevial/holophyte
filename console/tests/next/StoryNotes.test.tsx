import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Status, StoryChildRow, StoryDecisionRow, StoryDetail, StoryWitnessRow } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, NOW, SERVE } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("story.json", SERVE)).json()) as StoryDetail;
const DAY = 86_400_000;

const child = (ticket: string, facts: Partial<StoryChildRow>): StoryChildRow => ({
  ...pinned.children[0]!, ticket, title: `${ticket} title`, depends_on: [], ...facts,
});

const demo12 = (facts: Partial<StoryDetail> = {}): StoryDetail => ({
  ...pinned,
  ticket: "DEMO-12",
  title: "Unattended runs recover themselves",
  children: [
    child("DEMO-5", { status: "merged", merged_ms: NOW - 3 * 3_600_000 }),
    child("DEMO-10", { status: "merged", merged_ms: NOW - 2 * 3_600_000 }),
    child("DEMO-7", { status: "in_flight", depends_on: ["DEMO-5"] }),
    child("DEMO-11", { status: "in_flight", depends_on: ["DEMO-10"] }),
    child("DEMO-2", { status: "ready", column: "ready" }),
    child("DEMO-1", { status: "ready", column: "backlog", depends_on: ["DEMO-7", "DEMO-11"] }),
  ],
  ...facts,
});

async function mount(story: StoryDetail) {
  const polls = peersFetch(DEMO, {
    [DEMO]: { status: { ...working, project: "/srv/dev/demo", runs: [], queued: [], now: NOW }, attention: { level: "none", now: NOW, project: "/srv/dev/demo", items: [] } },
  });
  const fetch: Fetch = async (url) => (url === `${DEMO}/stories/DEMO-12` ? Response.json(story) : polls(url));
  window.location.hash = "#/stories/demo/DEMO-12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("DEMO-12's step list holds its steps in order, says what runs side by side and what each card waits on", async () => {
  await mount(demo12());
  const steps = Array.from(document.querySelectorAll<HTMLElement>("[data-step]"));
  expect(steps.map((step) => step.getAttribute("aria-label"))).toEqual(["Step 1", "Step 2", "Step 3"]);
  const first = steps[0]!;
  expect(Array.from(first.querySelectorAll("[data-node]"), (card) => card.getAttribute("data-node"))).toEqual(["DEMO-5", "DEMO-10", "DEMO-2"]);
  expect(first.querySelector("h3")?.textContent).toBe("Step 13 can run side by side");
  expect(steps[2]!.querySelector("h3")?.textContent).toBe("Step 3");
  const after = (ticket: string) => document.querySelector(`[data-step] [data-node="${ticket}"] [data-after]`)?.textContent;
  expect(after("DEMO-1")).toBe("after DEMO-7, DEMO-11");
  expect(after("DEMO-2")).toBe("starts the story");
  const graph = document.querySelector("[data-graph]")!;
  expect(graph.classList.contains("story-wide")).toBe(true);
  expect(graph.closest(".story-graph")).not.toBeNull();
  expect(first.closest(".story-steps")?.closest(".story-graph")).not.toBeNull();
});

const decision = (facts: Partial<StoryDecisionRow>): StoryDecisionRow => ({ ...pinned.decisions[0]!, ...facts });
const witness = (facts: Partial<StoryWitnessRow>): StoryWitnessRow => ({ ...pinned.witnesses[0]!, ...facts });
const OCT_9 = new Date(2026, 9, 9, 14, 0).getTime();

test("the Decisions and witnesses panel counts its lines and lists them newest first by kind, text and time", async () => {
  await mount(demo12({
    decisions: [
      decision({ id: 1, question: "Requeue or cancel a silent lease?", answer: "requeue", answered_by: "maintainer", at: OCT_9 - DAY, answered_ms: OCT_9 }),
      decision({ id: 2, question: "Replan around DEMO-11?", answer: null, answered_by: null, answered_ms: null, at: OCT_9 - 2 * DAY }),
    ],
    witnesses: [
      witness({ key: "W2", criterion: "A stale lease pages once", verdict: null, main_sha: null, at: null }),
      witness({ key: "W1", criterion: "Exports survive a restart", verdict: "green", main_sha: "5acc138e", at: OCT_9 - 3 * DAY }),
    ],
  }));
  const panel = screen.getByRole("region", { name: /^Decisions and witnesses/ });
  expect(panel.querySelector("[data-note-count]")?.textContent).toBe("4");
  const rows = Array.from(panel.querySelectorAll("li"), (row) => Array.from(row.children, (cell) => cell.textContent));
  expect(rows).toEqual([
    ["DECISION", "Requeue or cancel a silent lease? → requeue", "maintainer · Oct 9"],
    ["DECISION", "Replan around DEMO-11?", "open · needs you"],
    ["WITNESS", "W1 · Exports survive a restart", "green at 5acc138 · Oct 6"],
    ["WITNESS", "W2 · A stale lease pages once", "not run yet"],
  ]);
  const tones = Array.from(panel.querySelectorAll("[data-note-kind]"), (kind) => ["text-act", "text-need", "text-live", "text-ink-3"].find((tone) => kind.classList.contains(tone)));
  expect(tones).toEqual(["text-act", "text-need", "text-live", "text-ink-3"]);
});

test("a story with no decisions and no witnesses says so", async () => {
  await mount(demo12({ decisions: [], witnesses: [] }));
  const panel = screen.getByRole("region", { name: /^Decisions and witnesses/ });
  expect(panel.querySelector("[data-note-count]")?.textContent).toBe("0");
  expect(panel.querySelector("p")?.textContent).toBe("No decisions or witnesses yet.");
});
