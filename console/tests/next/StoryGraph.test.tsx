import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Status, StoryDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { StoryGraph } from "../../src/next/components/StoryGraph";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import type { State } from "../../src/next/state";
import { layoutGraph, type StoryNode } from "../../src/next/story";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, NOW, SERVE } from "./needsFleet";

const node = (ticket: string, depends_on: string[] = [], facts: Partial<StoryNode> = {}): StoryNode => ({
  ticket, title: `${ticket} title`, run: null, time_box_ms: null, state: "todo", word: "to do", kind: "ready", depends_on, ...facts,
});
const columns = (nodes: StoryNode[]) => layoutGraph(nodes).columns.map((column) => column.map((each) => each.ticket));

test("layoutGraph puts each node one column right of its deepest dependency and sizes the graph", () => {
  const diamond = [node("A"), node("B", ["A"]), node("C", ["A"]), node("D", ["B", "C"])];
  const graph = layoutGraph(diamond);
  expect(columns(diamond)).toEqual([["A"], ["B", "C"], ["D"]]);
  expect(graph.at.get("B")).toEqual({ x: 284, y: 0 });
  expect(graph.at.get("C")).toEqual({ x: 284, y: 124 });
  expect(graph.at.get("D")?.x).toBe(568);
  expect([graph.width, graph.height]).toEqual([780, 232]);
  expect(columns([...diamond, node("E", ["A", "D"])])[3]).toEqual(["E"]);
  const cycle = [node("A", ["D"]), ...diamond.slice(1)];
  expect(columns(cycle).flat().sort()).toEqual(["A", "B", "C", "D"]);
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
const cardOf = (ticket: string) => document.querySelector(`[data-node="${ticket}"]`)!;

afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("DEMO-12's graph draws six cards and four arrows, faint only where both ends merged", () => {
  const { unmount } = render(<StoryGraph nodes={demo12()} base={SEEDED_BASE} />);
  expect(document.querySelectorAll("[data-card]").length).toBe(6);
  expect(edgesIn("open").length).toBe(4);
  expect(edgesIn("dim")).toEqual([]);
  const one = cardOf("DEMO-1");
  expect((one as HTMLElement).style.left).toBe("568px");
  expect(one.getAttribute("aria-label")).toEndWith("Waits on DEMO-7, DEMO-11");
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

test("the story page holds the graph's legend, its focusable scroll region and its key line", async () => {
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
  expect(screen.getByRole("region", { name: "Story graph, scrolls sideways when wide" }).getAttribute("tabindex")).toBe("0");
  expect(Array.from(document.querySelectorAll("[data-node]"), (card) => card.getAttribute("data-node"))).toEqual(["KO-12", "KO-13"]);
  expect(document.querySelector("[data-graph-key]")?.textContent).toContain("the ticket at the arrow waits on the one it comes from");
});
