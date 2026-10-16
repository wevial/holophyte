import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Attention, Status, StoryChildRow, StoryDetail } from "../../src/lib/contract.gen";
import { addressOf, type HostRecord } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient, type ContractBodies } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { storyNodes } from "../../src/next/story";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, failed, NOW, run, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("story.json", SERVE)).json()) as StoryDetail;
const template = pinned.children[0]!;

const child = (ticket: string, facts: Partial<StoryChildRow>): StoryChildRow => ({
  ...template, ticket, title: `${ticket} title`, depends_on: [], ...facts,
});
const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();

const demo12: StoryDetail = {
  ...pinned,
  ticket: "DEMO-12",
  title: "Unattended runs recover themselves",
  children: [
    child("DEMO-5", { status: "merged", merged_ms: at(8, 47) }),
    child("DEMO-10", { status: "merged", merged_ms: at(9, 30) }),
    child("DEMO-7", { status: "in_flight", run: 7, depends_on: ["DEMO-5"] }),
    child("DEMO-11", { status: "in_flight", run: 11, depends_on: ["DEMO-10"] }),
    child("DEMO-2", { status: "ready", column: "ready" }),
    child("DEMO-1", { status: "ready", column: "backlog", depends_on: ["DEMO-7", "DEMO-11"] }),
  ],
};

const reviewing = { ...run(7, "DEMO-7"), phase: "reviewing", round: 1, max_rounds: 3 };
const demoStatus: Status = { ...working, project: "/srv/dev/demo", runs: [reviewing], queued: [], now: NOW };
const demoAttention: Attention = { level: "attention", now: NOW, project: "/srv/dev/demo", items: [failed[0]!] } as Attention;

/** `demo` on DEMO and `writer` on WRITER; `demo` answers `/stories/DEMO-12`
 *  with `story`, or 404 when it is null. Every url asked is kept. */
function fleet(story: StoryDetail | null) {
  const asked: string[] = [];
  const polls = peersFetch(DEMO, {
    [DEMO]: { status: demoStatus, attention: demoAttention },
    [WRITER]: { status: { ...working, project: "/srv/dev/writer", runs: [], queued: [], now: NOW }, attention: { level: "none", now: NOW, project: "/srv/dev/writer", items: [] } },
  });
  const fetch: Fetch = async (url) => {
    asked.push(url);
    if (url === `${DEMO}/stories/DEMO-12`) return story ? Response.json(story) : new Response("not found", { status: 404 });
    return polls(url);
  };
  return { fetch, asked };
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
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const text = (selector: string) => document.querySelector(selector)?.textContent;
const segments = () => Array.from(document.querySelectorAll("[data-segment]"), (segment) => segment.getAttribute("data-segment"));
const chipState = () => document.querySelector("[data-story-chip] [data-state]")?.getAttribute("data-state");

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the story header names the story, links its project's tickets and counts its children by state", async () => {
  await mount("#/stories/demo/DEMO-12", fleet(demo12).fetch);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Unattended runs recover themselves");
  const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
  expect(crumb.textContent).toBe("demo tickets / Stories / DEMO-12");
  expect(screen.getByRole("link", { name: "demo tickets" }).getAttribute("href")).toEndWith("#/tickets?project=demo");
  expect(text("[data-story-chip]")).toBe("Needs you");
  expect(chipState()).toBe("needs_you");
  expect(text("[data-merged]")).toBe("2 of 6 merged");
  expect(segments()).toEqual(["done", "done", "flight", "need", "ready", "backlog"]);
  expect(text("[data-counts]")).toBe("1 in flight · 1 needs you");
});

const demoHost: HostRecord<ContractBodies> = {
  key: addressOf(DEMO), address: addressOf(DEMO), base: DEMO, label: addressOf(DEMO), project: demoStatus.project,
  status: demoStatus, attention: demoAttention, polled_ms: NOW, seen_ms: NOW, error: null, needs_token: false,
};
const placed = (story: StoryDetail) => Object.fromEntries(storyNodes(story, demoHost, NOW, new Map()).map((node) => [node.ticket, [node.state, node.word]]));

test("each child's state comes from its merge, its live run, its attention item, then its board facts", () => {
  expect(placed(demo12)).toEqual({
    "DEMO-5": ["merged", "merged 08:47"],
    "DEMO-10": ["merged", "merged 09:30"],
    "DEMO-7": ["running", "reviewing · round 2 of 3"],
    "DEMO-11": ["failed", "failed"],
    "DEMO-2": ["queued", "queued"],
    "DEMO-1": ["backlog", "backlog"],
  });
  const promoted = { ...demo12, children: demo12.children.map((each) => (each.ticket === "DEMO-1" ? { ...each, column: "ready" as const } : each)) };
  expect(placed(promoted)["DEMO-1"]).toEqual(["todo", "waiting on DEMO-7, DEMO-11"]);
});

test("a live run whose host has not answered since its heartbeat went past the threshold reads stale", () => {
  const quietHost = { ...demoHost, seen_ms: NOW - 280_000 };
  const node = storyNodes(demo12, quietHost, NOW, new Map()).find((each) => each.ticket === "DEMO-7")!;
  expect([node.state, node.kind]).toEqual(["stale", "need"]);
});

test("a merge before today is worded by its day", () => {
  const older = { ...demo12, children: [child("DEMO-5", { status: "merged", merged_ms: at(8, 47) - 86_400_000 }), child("DEMO-10", { status: "merged", merged_ms: new Date(2026, 9, 8, 15, 0).getTime() })] };
  expect(placed(older)).toEqual({ "DEMO-5": ["merged", "merged yesterday"], "DEMO-10": ["merged", "merged Oct 8"] });
});

test("a node keeps its edges to drawn children only, dropping an abandoned child's", () => {
  const story = {
    ...demo12,
    children: [
      child("DEMO-5", { status: "merged", merged_ms: at(8, 47) }),
      child("DEMO-1", { status: "abandoned", column: "canceled" }),
      child("DEMO-2", { status: "ready", column: "ready", depends_on: ["DEMO-5", "DEMO-1", "DEMO-40"] }),
    ],
  };
  const edges = Object.fromEntries(storyNodes(story, demoHost, NOW, new Map()).map((node) => [node.ticket, node.depends_on]));
  expect(edges).toEqual({ "DEMO-5": [], "DEMO-2": ["DEMO-5"] });
});

test("a story whose children all merged but one abandoned is done, the abandoned child in no count", async () => {
  const done: StoryDetail = {
    ...demo12,
    children: [
      child("DEMO-5", { status: "merged", merged_ms: at(8, 47) }),
      child("DEMO-10", { status: "merged", merged_ms: at(9, 30) }),
      child("DEMO-2", { status: "merged", merged_ms: at(10, 0) }),
      child("DEMO-1", { status: "abandoned", column: "canceled" }),
    ],
  };
  await mount("#/stories/demo/DEMO-12", fleet(done).fetch);
  expect(text("[data-story-chip]")).toBe("Done");
  expect(chipState()).toBe("merged");
  expect(text("[data-merged]")).toBe("3 of 3 merged");
  expect(segments()).toEqual(["done", "done", "done"]);
  expect(document.querySelector("[data-counts]")).toBeNull();
});

test("the story is asked of its project's daemon only", async () => {
  const { fetch, asked } = fleet(demo12);
  await mount("#/stories/demo/DEMO-12", fetch);
  expect(asked.filter((url) => url.includes("/stories/"))).toEqual([`${DEMO}/stories/DEMO-12`]);
});

test("a story its project's daemon answers 404 reads as missing", async () => {
  await mount("#/stories/demo/DEMO-12", fleet(null).fetch);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("No story DEMO-12 in demo.");
  expect(document.querySelector("[data-story-chip]")).toBeNull();
});

test("a project no host serves reads as missing and asks no host for the story", async () => {
  const { fetch, asked } = fleet(demo12);
  await mount("#/stories/nowhere/DEMO-12", fetch);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("No story DEMO-12 in nowhere.");
  expect(asked.filter((url) => url.includes("/stories/"))).toEqual([]);
});

test("the story page spans the content width, keeps its notes at 1320 px and words the graph top to bottom", async () => {
  await mount("#/stories/demo/DEMO-12", fleet(demo12).fetch);
  const page = document.getElementById("page-title")!.closest("section")!.parentElement!;
  expect(Array.from(page.classList).filter((name) => name.startsWith("max-w") || name === "mx-auto")).toEqual([]);
  expect(screen.getByRole("region", { name: /^Decisions and witnesses/ }).classList.contains("max-w-[1320px]")).toBe(true);
  expect(text("[data-graph-key]")).toContain("one row runs in parallel");
  expect(page.lastElementChild!.textContent).toStartWith("Tickets with no dependency start the graph at the top.");
});
