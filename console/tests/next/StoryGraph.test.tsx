import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Status, StoryDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { StoryGraph } from "../../src/next/components/StoryGraph";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import type { State } from "../../src/next/state";
import { arrowPath, layoutGraph, type StoryNode } from "../../src/next/story";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, NOW, SERVE } from "./needsFleet";

const node = (ticket: string, depends_on: string[] = [], facts: Partial<StoryNode> = {}): StoryNode => ({
  ticket, title: `${ticket} title`, run: null, time_box_ms: null, state: "todo", word: "to do", kind: "ready", depends_on, ...facts,
});
const steps = (nodes: StoryNode[]) => layoutGraph(nodes).steps.map((step) => step.map((each) => each.ticket));

test("layoutGraph puts each node one step past its deepest dependency, in board order within a step", () => {
  const diamond = [node("A"), node("C", ["A"]), node("B", ["A"]), node("D", ["B", "C"])];
  expect(steps(diamond)).toEqual([["A"], ["C", "B"], ["D"]]);
  expect(steps([...diamond, node("E", ["A", "D"])])[3]).toEqual(["E"]);
  const cycle = [node("A", ["D"]), ...diamond.slice(1)];
  expect(steps(cycle).flat().sort()).toEqual(["A", "B", "C", "D"]);
});

test("an arrow runs from its dependency's bottom centre to the waiting card's top centre, its head pointing down", () => {
  const numbers = (text: string) => text.trim().split(/[\s,]+/).map(Number);
  const path = arrowPath({ left: 128, top: 0, width: 340, height: 112 }, { left: 524, top: 164, width: 340, height: 112 });
  const [curve, head] = path.split("M").filter(Boolean);
  const [start, ...controls] = curve!.split("C");
  expect(numbers(start!)).toEqual([298, 112]);
  expect(numbers(controls[0]!).slice(-2)).toEqual([694, 164]);
  const [armA, tip, armB] = head!.split("L").map(numbers);
  expect(tip).toEqual([694, 164]);
  for (const arm of [armA!, armB!]) expect(arm[1]).toBeLessThan(164);
  expect(armA![0]).toBeLessThan(694);
  expect(armB![0]).toBeGreaterThan(694);
});

const SEEDED_BASE = "http://127.0.0.1:8765";
const MERGED = { state: "merged" as State, word: "merged 08:47", kind: "done" as const };
const demo12 = (overrides: Record<string, Partial<StoryNode>> = {}): StoryNode[] => [
  node("DEMO-5", [], MERGED),
  node("DEMO-10", [], MERGED),
  node("DEMO-7", ["DEMO-5"], { state: "running", word: "reviewing · round 2 of 3", kind: "flight", run: 7, time_box_ms: 25 * 60_000 }),
  node("DEMO-11", ["DEMO-10"], { state: "failed", word: "failed", kind: "need", run: 11 }),
  node("DEMO-2", [], { state: "queued", word: "queued", kind: "ready" }),
  node("DEMO-1", ["DEMO-7", "DEMO-11"], { state: "backlog", word: "backlog", kind: "backlog" }),
].map((each) => ({ ...each, ...overrides[each.ticket] }));

const edgesIn = (group: "dim" | "open") =>
  Array.from(document.querySelectorAll(`[data-edges="${group}"] path`), (path) => path.getAttribute("data-edge"));
const GRAPH = "[data-graph]";
const cardOf = (ticket: string) => document.querySelector(`${GRAPH} [data-node="${ticket}"]`)!;

afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("DEMO-12's graph holds a labelled row per step, its 340 px cards on a wrapping line in board order", () => {
  render(<StoryGraph nodes={demo12()} base={SEEDED_BASE} />);
  const rows = Array.from(document.querySelectorAll<HTMLElement>(`${GRAPH} [data-row]`));
  expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual(["Step 1", "Step 2", "Step 3"]);
  expect(rows.map((row) => Array.from(row.querySelectorAll("[data-node]"), (card) => card.getAttribute("data-node")))).toEqual([
    ["DEMO-5", "DEMO-10", "DEMO-2"],
    ["DEMO-7", "DEMO-11"],
    ["DEMO-1"],
  ]);
  expect(rows.map((row) => row.querySelector("[data-step-note]")?.textContent ?? null)).toEqual(["3 side by side", "2 side by side", null]);
  for (const row of rows) {
    expect(row.textContent).toStartWith(`Step ${row.dataset.row}`);
    expect(row.querySelector("[data-step-cards]")!.classList.contains("flex-wrap")).toBe(true);
  }
  for (const card of document.querySelectorAll(`${GRAPH} [data-node]`)) expect(card.classList.contains("w-[340px]")).toBe(true);
  expect(cardOf("DEMO-1").getAttribute("aria-label")).toEndWith("Waits on DEMO-7, DEMO-11");
  expect(document.querySelectorAll(`${GRAPH} [data-band]`).length).toBe(2);
});

test("a story that starts with one ticket says so beside Step 1", () => {
  render(<StoryGraph nodes={[node("A"), node("B", ["A"])]} base={SEEDED_BASE} />);
  const notes = Array.from(document.querySelectorAll(`${GRAPH} [data-row]`), (row) => row.querySelector("[data-step-note]")?.textContent ?? null);
  expect(notes).toEqual(["starts the story", null]);
});

test("a card shows a long title whole, with no line clamp", () => {
  const title = "Requeue a run whose lease went silent, page the operator once, and keep the run's worktree, branch and review rounds intact for the retry";
  expect(title.length).toBeGreaterThan(120);
  render(<StoryGraph nodes={[node("A", [], { title })]} base={SEEDED_BASE} />);
  const cards = Array.from(document.querySelectorAll('[data-node="A"]'));
  expect(cards.map((card) => (card.closest("[data-graph]") != null ? "graph" : card.closest("[data-step]") != null ? "steps" : "other"))).toEqual(["graph", "steps"]);
  for (const card of cards) {
    const shown = Array.from(card.querySelectorAll("span")).find((span) => span.textContent === title)!;
    expect(shown).toBeDefined();
    expect(Array.from(shown.classList).filter((name) => name.startsWith("line-clamp") || name === "truncate")).toEqual([]);
    expect(card.classList.contains("overflow-hidden")).toBe(false);
  }
});

test("DEMO-12's graph draws four open arrows, faint only where both ends merged, and nothing in it scrolls sideways", () => {
  const { unmount } = render(<StoryGraph nodes={demo12()} base={SEEDED_BASE} />);
  expect(edgesIn("open")).toEqual(["DEMO-5 DEMO-7", "DEMO-10 DEMO-11", "DEMO-7 DEMO-1", "DEMO-11 DEMO-1"]);
  expect(edgesIn("dim")).toEqual([]);
  const graph = screen.getByRole("region", { name: "Dependency graph" });
  const all = [graph, ...graph.querySelectorAll("*")];
  const sideways = ["overflow-x-auto", "overflow-x-scroll", "overflow-auto", "overflow-scroll", "fade-l", "fade-r", "story-scroll"];
  expect(all.filter((element) => sideways.some((name) => element.classList.contains(name)))).toEqual([]);
  expect(all.filter((element) => /sideways|scroll/i.test(element.getAttribute("aria-label") ?? ""))).toEqual([]);
  expect(graph.querySelectorAll("[tabindex]").length).toBe(0);
  unmount();

  render(<StoryGraph nodes={demo12({ "DEMO-7": MERGED })} base={SEEDED_BASE} />);
  expect(edgesIn("dim")).toEqual(["DEMO-5 DEMO-7"]);
  expect(edgesIn("open").length).toBe(3);
});

test("each card takes its state's look and mark, links its run when it has one and shows its time box", () => {
  render(<StoryGraph nodes={demo12()} base={SEEDED_BASE} />);
  const looks = ["DEMO-5", "DEMO-7", "DEMO-11", "DEMO-2", "DEMO-1"].map((ticket) => cardOf(ticket).querySelector("[data-card]")!.getAttribute("data-card"));
  expect(looks).toEqual(["merged", "live", "todo", "todo", "backlog"]);
  expect(cardOf("DEMO-11").querySelector("[data-card]")!.getAttribute("data-mark")).toBe("fail");
  expect(cardOf("DEMO-2").querySelector("[data-card]")!.getAttribute("data-mark")).toBe("wait");
  const running = cardOf("DEMO-7");
  expect(running.tagName).toBe("A");
  expect(running.getAttribute("href")).toEndWith(`#run=7&daemon=${encodeURIComponent(SEEDED_BASE)}`);
  expect(running.querySelector("[data-time-box]")?.textContent).toBe("25m");
  expect(cardOf("DEMO-1").tagName).not.toBe("A");
  expect(screen.queryByRole("link", { name: /^DEMO-1 / })).toBeNull();
});

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("story.json", SERVE)).json()) as StoryDetail;
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));

test("the story page holds the graph's legend, its cards and its key line", async () => {
  const polls = peersFetch(DEMO, {
    [DEMO]: { status: { ...working, project: "/srv/dev/demo", runs: [], queued: [], now: NOW }, attention: { level: "none", now: NOW, project: "/srv/dev/demo", items: [] } },
  });
  const fetch: Fetch = async (url) => (url === `${DEMO}/stories/DEMO-12` ? Response.json({ ...pinned, ticket: "DEMO-12" }) : polls(url));
  window.location.hash = "#/stories/demo/DEMO-12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
  const legend = screen.getByRole("list", { name: "Card states" });
  expect(legend.querySelectorAll('[role="listitem"]').length).toBe(7);
  expect(Array.from(document.querySelectorAll(`${GRAPH} [data-node]`), (card) => card.getAttribute("data-node"))).toEqual(["KO-12", "KO-13"]);
  expect(document.querySelector("[data-graph-key]")?.textContent).toContain("the ticket at the arrow waits on the one it comes from");
});
