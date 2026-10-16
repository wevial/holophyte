import { afterAll, afterEach, beforeAll, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, BoardTicket, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { blocked, ciPark, DEMO, failed, merged, MINUTE, NOW, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;

type State = BoardColumn["state"];

function boardOf(keys: Partial<Record<State, string[]>>, facts: Record<string, Partial<BoardTicket>> = {}): Board {
  const states: State[] = ["backlog", ...pinned.columns.map((column) => column.state)];
  return {
    ...pinned,
    columns: states.map((state) => ({
      state,
      tickets: (keys[state] ?? []).map((ticket) => ({ ...template, ticket, title: `${ticket} title`, ...facts[ticket] })),
    })),
  };
}

interface Daemon {
  board?: Board;
  items?: typeof blocked[];
  shipped?: ShippedRow[];
}

function fleet(demo: Daemon, writer: Daemon): Fetch {
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: [], queued: [], admission: "enabled", now: NOW },
    attention: { level: body.items?.length ? "attention" : "none", now: NOW, project, items: body.items ?? [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo", demo), [WRITER]: daemon("/srv/dev/writer", writer) });
  return async (url) => {
    for (const [base, body] of [[DEMO, demo], [WRITER, writer]] as const) {
      if (url === `${base}/board`) return body.board ? Response.json(body.board) : new Response("not found", { status: 404 });
      if (url === `${base}/shipped?limit=50`) return Response.json({ rows: body.shipped ?? [], next_before: null, limit: 50 });
    }
    return polls(url);
  };
}

const seeded = fleet(
  {
    board: boardOf({ backlog: ["DEMO-1"], ready: ["DEMO-2"], blocked_on_deps: ["DEMO-5"], in_flight: ["DEMO-3", "DEMO-11"], blocked_on_operator: ["DEMO-8", "DEMO-4"] }, {
      "DEMO-3": { time_box_ms: 25 * MINUTE },
    }),
    items: [blocked, ...failed, ciPark],
    shipped: [merged(10, "DEMO-10", NOW - 30 * MINUTE), merged(7, "DEMO-7", NOW - 13 * 60 * MINUTE)],
  },
  { board: boardOf({ ready: ["WRITER-9"] }) },
);

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

const view = () => within(screen.getByRole("group", { name: "View" }));
const cells = (row: Element) => Array.from(row.querySelectorAll("[data-cell]"), (cell) => cell.textContent);
const rowsIn = (root: ParentNode = document) => Array.from(root.querySelectorAll<HTMLElement>("[data-lane-rows] > li > a"));
const keysIn = (root?: ParentNode) => rowsIn(root).map((row) => row.querySelector("[data-cell=key]")?.textContent);
const rowOf = (key: string) => rowsIn().find((row) => row.querySelector("[data-cell=key]")?.textContent === key)!;
const peek = () => screen.queryByRole("dialog");

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
  sessionStorage.clear();
  localStorage.clear();
});

test("Table draws one row per ticket, Needs you then In flight, Ready, Backlog and Done today, and a remount keeps it", async () => {
  await mount("#/tickets?project=demo", seeded);
  expect(view().getByRole("button", { name: "Board" }).getAttribute("aria-pressed")).toBe("true");
  await press("Table");
  const heads = document.querySelector("[data-table-heads]")!;
  expect(Array.from(heads.children, (head) => head.textContent)).toEqual(["Status", "Key", "Title", "Stage", "Est."]);
  expect(rowsIn().map(cells)).toEqual([
    ["Needs you", "DEMO-4", "DEMO-4 title", "question open 8h 02m", ""],
    ["Needs you", "DEMO-11", "DEMO-11 title", "failed · verify failed", ""],
    ["In flight", "DEMO-8", "DEMO-8 title", "on CI · PR #10", ""],
    ["In flight", "DEMO-3", "DEMO-3 title", "running", "25m time box"],
    ["Ready", "DEMO-5", "DEMO-5 title", "to do", ""],
    ["Ready", "DEMO-2", "DEMO-2 title", "queued", ""],
    ["Backlog", "DEMO-1", "DEMO-1 title", "backlog", ""],
    ["Done today", "DEMO-10", "Approved ticket", "merged 11:30", ""],
  ]);
  expect(document.querySelector("[data-lane-grid]")).toBeNull();
  cleanup();

  await mount("#/tickets?project=demo", seeded);
  expect(view().getByRole("button", { name: "Table" }).getAttribute("aria-pressed")).toBe("true");
  expect(keysIn()).toHaveLength(8);
});

test("under 820 px the heading row hides and a row's title wraps onto its own line under status and key", async () => {
  await mount("#/tickets?project=demo", seeded);
  await press("Table");
  const classes = (element: Element | null) => Array.from(element?.classList ?? []);
  expect(classes(document.querySelector("[data-table-heads]"))).toContain("max-[820px]:hidden");
  const row = rowOf("DEMO-3");
  expect(classes(row)).toContain("max-[820px]:grid-cols-[auto_auto_minmax(0,1fr)]");
  expect(classes(row.querySelector("[data-cell=title]"))).toEqual(expect.arrayContaining(["max-[820px]:col-span-full", "max-[820px]:row-start-2", "max-[820px]:whitespace-normal"]));
  for (const cell of ["stage", "estimate"]) expect(classes(row.querySelector(`[data-cell=${cell}]`))).toContain("max-[820px]:hidden");
});

test("Table grouped by Project draws a lane per project with its heading, and folding one hides only its rows", async () => {
  await mount("#/tickets", seeded);
  await press("Table");
  await press("Project");
  const demo = screen.getByRole("region", { name: "demo" });
  const writer = screen.getByRole("region", { name: "writer" });
  expect(within(demo).getByRole("heading", { level: 2 }).querySelector("[data-lane-meta]")?.textContent).toBe("8 tickets");
  expect(keysIn(demo)).toEqual(["DEMO-4", "DEMO-11", "DEMO-8", "DEMO-3", "DEMO-5", "DEMO-2", "DEMO-1", "DEMO-10"]);
  expect(keysIn(writer)).toEqual(["WRITER-9"]);

  fireEvent.click(within(demo).getByRole("button", { expanded: true }));
  await act(settle);
  expect((demo.querySelector("[data-lane-rows]") as HTMLElement).hidden).toBe(true);
  expect((writer.querySelector("[data-lane-rows]") as HTMLElement).hidden).toBe(false);
});

test("a row's click opens the peek on its ticket and marks the row open, Esc returns focus to it, and a Ctrl click opens none", async () => {
  await mount("#/tickets?project=demo", seeded);
  await press("Table");
  let notPrevented = false;
  await act(async () => {
    notPrevented = fireEvent.click(rowOf("DEMO-3"), { ctrlKey: true });
  });
  expect(notPrevented).toBe(true);
  expect(peek()).toBeNull();

  await act(async () => void fireEvent.click(rowOf("DEMO-3")));
  expect(within(peek()!).getByText("DEMO-3")).toBeTruthy();
  expect(rowOf("DEMO-3").hasAttribute("data-open")).toBe(true);
  expect(rowOf("DEMO-2").hasAttribute("data-open")).toBe(false);
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  expect(peek()).toBeNull();
  expect(document.activeElement).toBe(rowOf("DEMO-3"));
});

test("with a storage that throws, pressing Table still draws the table", async () => {
  const reads = spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("storage disabled");
  });
  const writes = spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("storage disabled");
  });
  try {
    await mount("#/tickets?project=demo", seeded);
    await press("Table");
    expect(view().getByRole("button", { name: "Table" }).getAttribute("aria-pressed")).toBe("true");
    expect(keysIn()).toHaveLength(8);
  } finally {
    reads.mockRestore();
    writes.mockRestore();
  }
});
