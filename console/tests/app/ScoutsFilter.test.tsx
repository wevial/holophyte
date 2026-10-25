import { afterAll, afterEach, beforeAll, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ScoutDetail, ScoutRow, Scouts } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, SERVE, WRITER } from "./needsFleet";

const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const swarming = servedScouts.scouts[1]!;
const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;

const scout = (id: number, phase: ScoutRow["phase"], minutesAgo: number, row: Partial<ScoutRow> = {}): ScoutRow => ({
  ...swarming, id, phase, created_ms: NOW - minutesAgo * MINUTE, ...row,
});

/** DEMO and WRITER answering `/scouts` with their rows, and DEMO `/scouts/31` with scout 31;
 *  WRITER without rows answers 404. */
function scoutFetch(demo: ScoutRow[], writer?: ScoutRow[]): Fetch {
  const polls = fleet({ items: [] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/scouts/31`) return Response.json({ ...served, scout: { ...served.scout, id: 31 } });
    if (url === `${DEMO}/scouts`) return Response.json({ ...servedScouts, scouts: demo });
    if (url === `${WRITER}/scouts` && writer) return Response.json({ ...servedScouts, scouts: writer });
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: true });
  };
}

const demo = [scout(31, "swarming", 1), scout(29, "queued", 3), scout(27, "done", 4)];
const writer = [scout(30, "planning", 2)];

const LOCK_TITLE = "Loop runs crash with database is locked";
const locks = [
  scout(31, "swarming", 1, { title: LOCK_TITLE, ask: "Why do loop runs crash?" }),
  scout(29, "queued", 3, { title: "Writer holds", ask: "Which writer holds the LOCK?" }),
  scout(27, "done", 4, { title: "Map the merge gate", ask: "How does the merge gate decide?" }),
];

async function mount(hash: string, fetch: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const settleAll = async () => {
  for (let round = 0; round < 4; round += 1) await act(settle);
};
const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  await settleAll();
};
const choose = async (name: string, value: string) => {
  await act(async () => void fireEvent.change(select(name), { target: { value } }));
  await settleAll();
};

/** Type `text` into Search and wait out its debounce. */
async function search(text: string) {
  fireEvent.change(field(), { target: { value: text } });
  await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
  await settleAll();
}

function wideScreen() {
  return spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches: query === "(min-width: 1400px)",
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as MediaQueryList);
}

const field = () => screen.getByRole("searchbox", { name: "Search scouts" }) as HTMLInputElement;
const select = (name: string) => screen.getByRole("combobox", { name }) as HTMLSelectElement;
const optionTexts = (name: string) => Array.from(select(name).options, (option) => option.textContent);
const groups = () => [...document.querySelectorAll("li[data-scout-group]")] as HTMLElement[];
const groupNames = () => groups().map((group) => group.querySelector("[data-lane-head]")!.firstElementChild!.textContent);
const rowsOf = (element: ParentNode) => [...element.querySelectorAll("[data-scout]")].map((row) => row.getAttribute("data-scout"));
const ids = () => rowsOf(document);
const countLine = () => document.querySelector("[data-count-line]")!.textContent;
const searchParam = (name: string) => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get(name);

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the narrow list draws one group per project, newest group first, under a lane head and with no id or project cell", async () => {
  await mount("#/scouts", scoutFetch(demo, writer));
  expect(groupNames()).toEqual(["demo", "writer"]);
  const [demoGroup, writerGroup] = groups();
  expect(demoGroup!.querySelector("[data-lane-head]")!.textContent).toBe("demo3 scouts");
  expect(rowsOf(within(demoGroup!).getByRole("list", { name: "demo scouts" }))).toEqual(["31", "29", "27"]);
  expect(writerGroup!.querySelector("[data-lane-head]")!.textContent).toBe("writer1 scout");
  expect(rowsOf(within(writerGroup!).getByRole("list", { name: "writer scouts" }))).toEqual(["30"]);
  expect(document.querySelector('[data-cell="id"], [data-cell="project"]')).toBeNull();
  const heads = [...document.querySelectorAll("[data-scout-table] > [aria-hidden] > span")].map((head) => head.textContent);
  expect(heads).toEqual(["", "Title", "Kind", "Lead", "Helpers", "Took"]);
  cleanup();
  await mount("#/scouts?project=demo", scoutFetch(demo, writer));
  expect(groupNames()).toEqual(["demo"]);
  expect(ids()).toEqual(["31", "29", "27"]);
});

test("the wide list draws the same groups with no id or project in its rows and the first group's first scout in the pane", async () => {
  const media = wideScreen();
  try {
    await mount("#/scouts", scoutFetch(demo, writer));
    expect(groupNames()).toEqual(["demo", "writer"]);
    expect(groups().map(rowsOf)).toEqual([["31", "29", "27"], ["30"]]);
    expect(document.querySelector('[data-scout-list] [data-cell="id"]')).toBeNull();
    const facts = [...document.querySelectorAll("[data-scout-row-facts]")].map((line) => line.textContent);
    expect(facts.filter((line) => /demo|writer/.test(line ?? ""))).toEqual([]);
    expect(document.querySelector("[data-scout-list] li[aria-current]")!.getAttribute("data-scout")).toBe("31");
    const pane = document.querySelector("[data-scout-pane]") as HTMLElement;
    expect(pane.contains(screen.getByRole("region", { name: "scout 31" }))).toBe(true);
  } finally {
    media.mockRestore();
  }
});

test("Search keeps scouts whose title or ask holds the text in any case, as q, and Escape brings every scout back", async () => {
  await mount("#/scouts", scoutFetch(locks));
  await search("lock");
  expect(ids()).toEqual(["31", "29"]);
  expect(searchParam("q")).toBe("lock");
  expect(countLine()).toStartWith("all projects · 2 scouts");
  const hash = window.location.hash;
  cleanup();
  await mount(hash, scoutFetch(locks));
  expect(field().value).toBe("lock");
  expect(ids()).toEqual(["31", "29"]);
  await act(async () => void fireEvent.keyDown(field(), { key: "Escape" }));
  await settleAll();
  expect(field().value).toBe("");
  expect(searchParam("q")).toBeNull();
  expect(ids()).toEqual(["31", "29", "27"]);
});

test("/ on the page focuses Search without typing a slash", async () => {
  await mount("#/scouts", scoutFetch(demo));
  document.body.focus();
  let typed = true;
  await act(async () => void (typed = fireEvent.keyDown(document.body, { key: "/" })));
  expect(typed).toBe(false);
  expect(document.activeElement).toBe(field());
  expect(field().value).toBe("");
});

test("the State and Kind selects narrow the list as filter and type, and a group left empty is not drawn", async () => {
  const kinds = demo.map((row) => (row.id === 29 ? { ...row, kind: "bug" as const } : row));
  await mount("#/scouts", scoutFetch(kinds, [...writer, scout(26, "done", 5, { kind: "bug" })]));
  expect(optionTexts("State")).toEqual(["All states", "Running", "Queued", "Done", "Stopped", "Failed"]);
  expect(optionTexts("Kind")).toEqual(["All kinds", "Bug", "Research", "Recon", "Guardrails"]);
  expect([select("State").selectedIndex, select("Kind").selectedIndex]).toEqual([0, 0]);
  await choose("State", "running");
  expect(searchParam("filter")).toBe("running");
  expect(searchParam("project")).toBeNull();
  expect(ids()).toEqual(["31", "30"]);
  await choose("State", "all");
  await choose("Kind", "bug");
  expect(searchParam("type")).toBe("bug");
  expect(searchParam("filter")).toBeNull();
  expect(groups().map(rowsOf)).toEqual([["29"], ["26"]]);
  expect(groupNames()).toEqual(["demo", "writer"]);
  await choose("State", "queued");
  expect(ids()).toEqual(["29"]);
  expect(groupNames()).toEqual(["demo"]);
});

test("a search matching no scout reads No scouts match alone, and an empty scope still names its project", async () => {
  await mount("#/scouts?q=nothing", scoutFetch(demo, writer));
  expect(screen.getAllByText("No scouts match")).toHaveLength(1);
  expect(groups()).toEqual([]);
  expect(document.querySelector("[data-scout-table]")).toBeNull();
  cleanup();
  await mount("#/scouts?project=demo", scoutFetch([]));
  expect(screen.getByText("No scouts in demo.")).toBeTruthy();
  expect(screen.queryByText("No scouts match")).toBeNull();
});

test("pressing a wide row and closing New scout keep q and filter, and a Scout button's kind leaves the Kind select alone", async () => {
  const media = wideScreen();
  try {
    await mount("#/scouts?q=lock&filter=running", scoutFetch(locks));
    await click(within(document.querySelector('[data-scout="31"]') as HTMLElement).getByRole("link"));
    expect([searchParam("scout"), searchParam("q"), searchParam("filter")]).toEqual(["demo/31", "lock", "running"]);
    await click(screen.getByRole("button", { name: "New scout" }));
    await click(within(screen.getByRole("form", { name: "New scout" })).getByRole("button", { name: "Cancel" }));
    expect([searchParam("q"), searchParam("filter")]).toEqual(["lock", "running"]);
    cleanup();
    await mount("#/scouts?new=demo&kind=bug&ask=ASK", scoutFetch(locks));
    const form = screen.getByRole("form", { name: "New scout" });
    const pressed = within(within(form).getByRole("group", { name: "Kind" })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true");
    expect(pressed.map((button) => button.textContent)).toEqual(["Bug"]);
    expect(select("Kind").selectedOptions[0]!.textContent).toBe("All kinds");
    expect(ids()).toEqual(["31", "29", "27"]);
  } finally {
    media.mockRestore();
  }
});
