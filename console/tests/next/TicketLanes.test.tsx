import { afterAll, afterEach, beforeAll, beforeEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, BoardStory, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { blocked, DEMO, merged, MINUTE, NOW, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;

type State = BoardColumn["state"];

function boardOf(keys: Partial<Record<State, string[]>>, stories: BoardStory[] = []): Board {
  const states: State[] = ["backlog", ...pinned.columns.map((column) => column.state)];
  return {
    ...pinned,
    stories,
    columns: states.map((state) => ({
      state,
      tickets: (keys[state] ?? []).map((ticket) => ({ ...template, ticket, title: `${ticket} title` })),
    })),
  };
}

interface Daemon {
  board: Board;
  items?: typeof blocked[];
  shipped?: ShippedRow[];
  admission?: string;
}

function fleet(demo: Daemon, writer: Daemon): Fetch {
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: [], queued: [], admission: body.admission ?? "enabled", now: NOW },
    attention: { level: body.items?.length ? "attention" : "none", now: NOW, project, items: body.items ?? [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo", demo), [WRITER]: daemon("/srv/dev/writer", writer) });
  return async (url) => {
    for (const [base, body] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (url === `${base}/board`) return Response.json(body.board);
      if (url === `${base}/shipped?limit=50`) return Response.json({ rows: body.shipped ?? [], next_before: null, limit: 50 });
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
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function press(name: string) {
  fireEvent.click(screen.getByRole("button", { name }));
  await act(settle);
}

const group = () => within(screen.getByRole("group", { name: "Group by" }));
const segments = () => group().getAllByRole("button").map((button) => button.textContent);
const pressed = (name: string) => group().getByRole("button", { name }).getAttribute("aria-pressed");
const lanes = () => screen.queryAllByRole("region").filter((region) => region.querySelector(":scope > [data-lane-grid]"));
const laneNamed = (name: string) => screen.getByRole("region", { name });
const headOf = (lane: HTMLElement) => within(lane).getByRole("heading", { level: 2 }).querySelector("button")!;
const metaOf = (lane: HTMLElement) => lane.querySelector("[data-lane-meta]")?.textContent;
const needOf = (lane: HTMLElement) => lane.querySelector("[data-lane-need]")?.textContent ?? null;
const keysIn = (lane: HTMLElement, column: string) =>
  within(within(lane).getByRole("list", { name: column })).queryAllByRole("article").map((card) => card.firstElementChild?.firstElementChild?.textContent);

const projects = fleet(
  { board: boardOf({ backlog: ["DEMO-1"], ready: ["DEMO-2"], in_flight: ["DEMO-3"], blocked_on_operator: ["DEMO-4"] }), items: [blocked], admission: "held" },
  { board: boardOf({ ready: ["WRITER-1"], in_flight: ["WRITER-2"] }) },
);

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("Group by Project draws a lane per project in fleet order with its count, held note and need text, and a reload keeps it", async () => {
  await mount("#/tickets", projects);
  expect(pressed("None")).toBe("true");
  await press("Project");
  expect(lanes().map((lane) => lane.getAttribute("aria-label"))).toEqual(["demo", "writer"]);
  const [demo, writer] = lanes() as [HTMLElement, HTMLElement];
  expect([metaOf(demo), needOf(demo)]).toEqual(["4 tickets · held", "1 need you"]);
  expect(keysIn(demo, "Needs you")).toEqual(["DEMO-4"]);
  expect([metaOf(writer), needOf(writer)]).toEqual(["2 tickets", null]);
  expect(keysIn(writer, "In flight")).toEqual(["WRITER-2"]);
  cleanup();

  await mount("#/tickets", projects);
  expect(pressed("Project")).toBe("true");
  expect(lanes()).toHaveLength(2);
});

test("Group by Story holds each story's children, merged ones in Done today, then a No story lane", async () => {
  const story = { ticket: "DEMO-20", title: "Title", children: ["DEMO-21", "DEMO-22", "DEMO-23"], merged: 1, total: 3 };
  const fetch = fleet(
    { board: boardOf({ ready: ["DEMO-22", "DEMO-24"], in_flight: ["DEMO-23"] }, [story]), shipped: [merged(21, "DEMO-21", NOW - 30 * MINUTE)] },
    { board: boardOf({}) },
  );
  await mount("#/tickets?project=demo", fetch);
  await press("Story");
  expect(lanes().map((lane) => lane.getAttribute("aria-label"))).toEqual(["DEMO-20 · Title", "No story"]);
  const storyLane = laneNamed("DEMO-20 · Title");
  expect(metaOf(storyLane)).toBe("3 tickets · 1 done today");
  expect([keysIn(storyLane, "Done today"), keysIn(storyLane, "Ready"), keysIn(storyLane, "In flight")]).toEqual([["DEMO-21"], ["DEMO-22"], ["DEMO-23"]]);
  const rest = laneNamed("No story");
  expect(metaOf(rest)).toBe("1 ticket");
  expect(keysIn(rest, "Ready")).toEqual(["DEMO-24"]);
});

test("a lane's heading folds its grid, and the fold-all link folds every lane then reads Expand all lanes", async () => {
  await mount("#/tickets", projects);
  await press("Project");
  const [demo, writer] = lanes() as [HTMLElement, HTMLElement];
  fireEvent.click(headOf(demo));
  await act(settle);
  expect(headOf(demo).getAttribute("aria-expanded")).toBe("false");
  expect((demo.querySelector("[data-lane-grid]") as HTMLElement).hidden).toBe(true);
  expect((writer.querySelector("[data-lane-grid]") as HTMLElement).hidden).toBe(false);

  await press("Collapse all lanes");
  expect(lanes().map((lane) => headOf(lane).getAttribute("aria-expanded"))).toEqual(["false", "false"]);
  expect(screen.getByRole("button", { name: "Expand all lanes" })).toBeTruthy();
  await press("Expand all lanes");
  expect(lanes().map((lane) => headOf(lane).getAttribute("aria-expanded"))).toEqual(["true", "true"]);
});

test("a picked project offers no Project grouping, None draws no lane heading or fold-all link, and a throwing storage still switches", async () => {
  localStorage.setItem("holophyte.next.tickets.group", "project");
  await mount("#/tickets?project=demo", projects);
  expect(segments()).toEqual(["Story", "None"]);
  expect(pressed("None")).toBe("true");
  expect(screen.queryAllByRole("heading", { level: 2 })).toEqual([]);
  expect(screen.queryByRole("button", { name: /all lanes$/ })).toBeNull();
  cleanup();

  const reads = spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("storage disabled");
  });
  const writes = spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("storage disabled");
  });
  try {
    await mount("#/tickets", projects);
    expect(segments()).toEqual(["Project", "Story", "None"]);
    await press("Story");
    expect(pressed("Story")).toBe("true");
    expect(lanes().map((lane) => lane.getAttribute("aria-label"))).toEqual(["No story"]);
  } finally {
    reads.mockRestore();
    writes.mockRestore();
  }
});
