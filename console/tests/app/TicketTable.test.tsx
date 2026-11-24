import { afterAll, afterEach, beforeAll, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { compile } from "tailwindcss";
import type { Board, BoardColumn, BoardTicket, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { declarations } from "../cssTokens";
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
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
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

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
  sessionStorage.clear();
  localStorage.clear();
});

test("Table draws one row per ticket, Needs you then In flight, Ready, Backlog and Done today, each by key, and a remount keeps it", async () => {
  await mount("#/tickets?project=demo", seeded);
  expect(view().getByRole("button", { name: "Board" }).getAttribute("aria-pressed")).toBe("true");
  await press("Table");
  const heads = document.querySelector("[data-table-heads]")!;
  expect(Array.from(heads.children, (head) => head.textContent)).toEqual(["Status", "Key", "Title", "Stage", "Est."]);
  for (const element of [heads, ...heads.children]) expect(element.className).not.toMatch(/\buppercase\b|\btracking-/);
  expect(rowsIn().map(cells)).toEqual([
    ["Needs you", "DEMO-4", "DEMO-4 title", "question open 8h 02m", ""],
    ["Needs you", "DEMO-11", "DEMO-11 title", "failed · verify failed", ""],
    ["In flight", "DEMO-3", "DEMO-3 title", "running", "25m time box"],
    ["In flight", "DEMO-8", "DEMO-8 title", "on CI · PR #10", ""],
    ["Ready", "DEMO-2", "DEMO-2 title", "queued", ""],
    ["Ready", "DEMO-5", "DEMO-5 title", "to do", ""],
    ["Backlog", "DEMO-1", "DEMO-1 title", "backlog", ""],
    ["Done today", "DEMO-10", "Approved ticket", "merged 11:30", ""],
  ]);
  expect(document.querySelector("[data-lane-grid]")).toBeNull();
  cleanup();

  await mount("#/tickets?project=demo", seeded);
  expect(view().getByRole("button", { name: "Table" }).getAttribute("aria-pressed")).toBe("true");
  expect(keysIn()).toHaveLength(8);
});

test("a Done today row draws the merged state as an outlined check circle", async () => {
  await mount("#/tickets?project=demo", seeded);
  await press("Table");
  const glyph = rowOf("DEMO-10").querySelector("[data-cell=status] [role=img]")!;
  expect([glyph.getAttribute("aria-label"), glyph.getAttribute("data-state")]).toEqual(["merged", "merged"]);
  expect(glyph.querySelector("circle")!.closest("[fill]")!.getAttribute("fill")).toBe("none");
  for (const mark of glyph.querySelectorAll("circle, path")) expect(mark.closest("[stroke]")!.getAttribute("stroke")).toBe("currentColor");
  expect(glyph.querySelector("path")!.getAttribute("d")).toBe("M5 8.3 7.1 10.4 11 6.2");
});

test("compact density gives a Table row an important 34 px floor, under its comfortable height", async () => {
  await mount("#/tickets?project=demo", seeded);
  await press("Table");
  const utilities = await compile("@theme { --spacing: 0.25rem; } @tailwind utilities;", { base: import.meta.dir });
  const floors = declarations(utilities.build(Array.from(rowOf("DEMO-3").classList))).filter((entry) => entry.name === "min-height");
  const floorsUnder = (density: string | null) => {
    const page = document.implementation.createHTMLDocument();
    page.documentElement.replaceChild(page.importNode(document.body, true), page.body);
    if (density != null) page.documentElement.setAttribute("data-density", density);
    const row = page.getElementById(rowOf("DEMO-3").id)!;
    const named = (selector: string) => selector.replace(/\.((?:\\.|[\w-])+)/g, (_, name: string) => (row.classList.contains(name.replace(/\\(.)/g, "$1")) ? "*" : ":not(*)"));
    return floors.filter((entry) => row.matches(named(entry.path.join(" "))));
  };
  const comfortable = floorsUnder(null);
  expect(floorsUnder("compact").filter((entry) => !comfortable.includes(entry)).map((entry) => entry.value)).toEqual(["34px !important"]);
  expect(comfortable.map((entry) => parseFloat(entry.value))).toEqual([expect.any(Number)]);
  expect(parseFloat(comfortable[0]!.value)).toBeGreaterThan(34);
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
  expect(keysIn(demo)).toEqual(["DEMO-4", "DEMO-11", "DEMO-3", "DEMO-8", "DEMO-2", "DEMO-5", "DEMO-1", "DEMO-10"]);
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

test("pressing a row's ⋯ and one of its items opens no peek, and pressing another row's ⋯ switches menus in one press", async () => {
  const user = userEvent.setup();
  const click = (target: Element) => user.click(target).then(() => act(settle));
  await mount("#/tickets?project=demo", seeded);
  await press("Table");
  await click(screen.getByRole("button", { name: "More for DEMO-3" }));
  expect(peek()).toBeNull();
  await click(within(screen.getByRole("menu", { name: "More for DEMO-3" })).getByRole("menuitem", { name: "Copy link" }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(peek()).toBeNull();

  await click(screen.getByRole("button", { name: "More for DEMO-3" }));
  expect(screen.getByRole("menu", { name: "More for DEMO-3" })).toBeTruthy();
  await click(screen.getByRole("button", { name: "More for DEMO-2" }));
  expect(screen.getAllByRole("menu")).toEqual([screen.getByRole("menu", { name: "More for DEMO-2" })]);
  expect(peek()).toBeNull();
});
