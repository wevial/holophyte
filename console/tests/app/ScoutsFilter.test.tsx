import { afterAll, afterEach, beforeAll, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

/** DEMO and WRITER answering `/scouts` with their rows, and DEMO `/scouts/N` with scout N;
 *  WRITER without rows answers 404. */
function scoutFetch(demo: ScoutRow[], writer?: ScoutRow[]): Fetch {
  const polls = fleet({ items: [] }, { items: [] });
  return async (url) => {
    const id = new RegExp(`^${DEMO}/scouts/(\\d+)$`).exec(url)?.[1];
    if (id != null) return Response.json({ ...served, scout: { ...served.scout, id: Number(id) } });
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
const key = async (element: Element, name: string) => {
  await act(async () => void fireEvent.keyDown(element, { key: name }));
  await settleAll();
};
/** Open the `name` picker and press its option labelled `label`. */
const choose = async (name: string, label: string) => {
  await click(picker(name));
  await click(within(screen.getByRole("listbox", { name })).getByRole("option", { name: label }));
};
/** The `name` picker's options and which of them is selected, read open and
 *  closed again with Escape. */
const listed = async (name: string) => {
  await click(picker(name));
  const options = within(screen.getByRole("listbox", { name })).getAllByRole("option");
  const read = { texts: options.map((option) => option.textContent), selected: options.filter((option) => option.getAttribute("aria-selected") === "true").map((option) => option.textContent) };
  await key(document.activeElement!, "Escape");
  return read;
};

/** Type `text` into Search and wait out its debounce. */
async function search(text: string) {
  fireEvent.change(field(), { target: { value: text } });
  await act(() => new Promise((resolve) => setTimeout(resolve, 300)));
  await settleAll();
}

function wideScreen() {
  return spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches: query === "(min-width: 1100px)",
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as MediaQueryList);
}

const field = () => screen.getByRole("searchbox", { name: "Search scouts" }) as HTMLInputElement;
const picker = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}: `) });
const groups = () => [...document.querySelectorAll("li[data-scout-group]")] as HTMLElement[];
const groupHeading = (group: HTMLElement) => within(group).getByRole("heading", { level: 2 });
const groupNames = () => groups().map((group) => groupHeading(group).textContent);
/** Each group's level-2 heading and the count beside it. */
const groupHeads = () => groups().map((group) => [groupHeading(group).textContent, groupHeading(group).parentElement!.querySelector("[data-count]")!.textContent]);
const rowsOf = (element: ParentNode) => [...element.querySelectorAll("[data-scout]")].map((row) => row.getAttribute("data-scout"));
const ids = () => rowsOf(document);
const pageCount = () => document.querySelector("[data-page-count]")!.textContent;
const searchParam = (name: string) => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get(name);

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the narrow list draws one group per project, newest group first, under a heading with its count and the table's column heads", async () => {
  await mount("#/scouts", scoutFetch(demo, writer));
  expect(groupHeads()).toEqual([["demo", "3"], ["writer", "1"]]);
  expect(document.querySelector("[data-lane-head]")).toBeNull();
  const [demoGroup, writerGroup] = groups();
  expect(rowsOf(within(demoGroup!).getByRole("list", { name: "demo scouts" }))).toEqual(["31", "29", "27"]);
  expect(rowsOf(within(writerGroup!).getByRole("list", { name: "writer scouts" }))).toEqual(["30"]);
  const heads = [...document.querySelectorAll("[data-scout-table] > [aria-hidden] > span")].map((head) => head.textContent);
  expect(heads).toEqual(["", "Title", "Kind", "Lead", "Helpers", "Took"]);
  expect(document.querySelector('[data-cell="id"], [data-cell="project"]')).toBeNull();
  cleanup();
  await mount("#/scouts?project=demo", scoutFetch(demo, writer));
  expect(groupNames()).toEqual(["demo"]);
  expect(groups().map(rowsOf)).toEqual([["31", "29", "27"]]);
});

test("the wide list draws the same headed groups with no id cell and the first group's first scout beside it", async () => {
  const media = wideScreen();
  try {
    await mount("#/scouts", scoutFetch(demo, writer));
    expect(groupHeads()).toEqual([["demo", "3"], ["writer", "1"]]);
    expect(document.querySelector("[data-lane-head]")).toBeNull();
    expect(groups().map(rowsOf)).toEqual([["31", "29", "27"], ["30"]]);
    expect(document.querySelector('[data-scout-list] [data-cell="id"]')).toBeNull();
    expect(document.querySelector("[data-scout-list] li[aria-current]")!.getAttribute("data-scout")).toBe("31");
    const page = document.querySelector("[data-scout-page]") as HTMLElement;
    expect(page.contains(screen.getByRole("region", { name: "scout 31" }))).toBe(true);
  } finally {
    media.mockRestore();
  }
});

test("Search keeps scouts whose title or ask holds the text in any case, as q, and Escape brings every scout back", async () => {
  await mount("#/scouts", scoutFetch(locks));
  await search("lock");
  expect(ids()).toEqual(["31", "29"]);
  expect(searchParam("q")).toBe("lock");
  expect(pageCount()).toStartWith("2 scouts");
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

test("the State and Kind pickers narrow the list as filter and type, and a group left empty is not drawn", async () => {
  const kinds = demo.map((row) => (row.id === 29 ? { ...row, kind: "bug" as const } : row));
  await mount("#/scouts", scoutFetch(kinds, [...writer, scout(26, "done", 5, { kind: "bug" })]));
  expect(await listed("State")).toEqual({ texts: ["All states", "Running", "Queued", "Done", "Stopped", "Failed"], selected: ["All states"] });
  expect(await listed("Kind")).toEqual({ texts: ["All kinds", "Bug", "Research", "Recon", "Guardrails"], selected: ["All kinds"] });
  await choose("State", "Running");
  expect(searchParam("filter")).toBe("running");
  expect(searchParam("project")).toBeNull();
  expect(ids()).toEqual(["31", "30"]);
  await choose("State", "All states");
  await choose("Kind", "Bug");
  expect(searchParam("type")).toBe("bug");
  expect(searchParam("filter")).toBeNull();
  expect(groups().map(rowsOf)).toEqual([["29"], ["26"]]);
  expect(groupNames()).toEqual(["demo", "writer"]);
  await choose("State", "Queued");
  expect(ids()).toEqual(["29"]);
  expect(groupNames()).toEqual(["demo"]);
  expect([picker("State").textContent, picker("Kind").textContent]).toEqual(["Queued", "Bug"]);
});

test("the Kind picker opens on ArrowDown, picks with Enter and closes on Escape unchanged, focus back on its button each time", async () => {
  await mount("#/scouts", scoutFetch(demo, writer));
  picker("Kind").focus();
  await key(picker("Kind"), "ArrowDown");
  expect(screen.getByRole("listbox", { name: "Kind" })).toBeTruthy();
  expect(document.activeElement!.textContent).toBe("All kinds");
  await key(document.activeElement!, "ArrowDown");
  expect(document.activeElement!.textContent).toBe("Bug");
  await key(document.activeElement!, "Enter");
  expect(searchParam("type")).toBe("bug");
  expect(screen.queryByRole("listbox", { name: "Kind" })).toBeNull();
  expect(document.activeElement).toBe(picker("Kind"));

  await key(picker("Kind"), "ArrowDown");
  expect(document.activeElement!.textContent).toBe("Bug");
  await key(document.activeElement!, "ArrowDown");
  await key(document.activeElement!, "Escape");
  expect(screen.queryByRole("listbox", { name: "Kind" })).toBeNull();
  expect(searchParam("type")).toBe("bug");
  expect(picker("Kind").textContent).toBe("Bug");
  expect(document.activeElement).toBe(picker("Kind"));
});

test("a search matching no scout reads No scouts match alone, on one line after an icon, and an empty scope still names its project", async () => {
  await mount("#/scouts?q=nothing", scoutFetch(demo, writer));
  expect(screen.getAllByText("No scouts match")).toHaveLength(1);
  const line = screen.getByText("No scouts match");
  expect([...line.childNodes].map((node) => node.nodeName)).toEqual(["svg", "#text"]);
  expect(groups()).toEqual([]);
  expect(document.querySelector("[data-scout-table]")).toBeNull();
  cleanup();
  await mount("#/scouts?project=demo", scoutFetch([]));
  expect(screen.getByText("No scouts in demo.")).toBeTruthy();
  expect(screen.queryByText("No scouts match")).toBeNull();
});

test("choosing another row under a search and a Kind keeps both on the scout's page, whose list still shows only matching rows", async () => {
  const media = wideScreen();
  const kinds = locks.map((row) => ({ ...row, kind: row.id === 27 ? ("research" as const) : ("bug" as const) }));
  try {
    await mount("#/scouts?q=lock&type=bug", scoutFetch([...kinds, scout(25, "done", 5, { title: "Lock-free reads", kind: "research" })]));
    expect(ids()).toEqual(["31", "29"]);
    await click(within(document.querySelector('[data-scout="29"]') as HTMLElement).getByRole("link"));
    expect(window.location.hash.split("?")[0]).toBe("#/scouts/demo/29");
    expect([searchParam("q"), searchParam("type")]).toEqual(["lock", "bug"]);
    expect(field().value).toBe("lock");
    expect(picker("Kind").textContent).toBe("Bug");
    expect(ids()).toEqual(["31", "29"]);
    expect(document.querySelector("[data-scout-list] li[aria-current]")!.getAttribute("data-scout")).toBe("29");
    await click(within(document.querySelector('[data-scout="31"]') as HTMLElement).getByRole("link"));
    expect(window.location.hash.split("?")[0]).toBe("#/scouts/demo/31");
    expect([searchParam("q"), searchParam("type")]).toEqual(["lock", "bug"]);
    expect(ids()).toEqual(["31", "29"]);
  } finally {
    media.mockRestore();
  }
});

test("closing New scout keeps q and filter, and a Scout button's kind leaves the Kind picker alone", async () => {
  const media = wideScreen();
  try {
    await mount("#/scouts?q=lock&filter=running", scoutFetch(locks));
    await click(screen.getByRole("button", { name: "New scout" }));
    await click(within(screen.getByRole("form", { name: "New scout" })).getByRole("button", { name: "Cancel" }));
    expect([searchParam("q"), searchParam("filter")]).toEqual(["lock", "running"]);
    cleanup();
    await mount("#/scouts?new=demo&kind=bug&ask=ASK", scoutFetch(locks));
    const form = screen.getByRole("form", { name: "New scout" });
    const pressed = within(within(form).getByRole("group", { name: "Kind" })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true");
    expect(pressed.map((button) => button.textContent)).toEqual(["Bug"]);
    expect(picker("Kind").textContent).toBe("All kinds");
    expect(ids()).toEqual(["31", "29", "27"]);
  } finally {
    media.mockRestore();
  }
});

test("a press on Search's input or its glyph while Kind is open closes it unchanged and gives focus back to the Kind button", async () => {
  await mount("#/scouts?type=bug", scoutFetch(demo, writer));
  for (const target of [field(), field().closest("label")!.querySelector("svg")!]) {
    await click(picker("Kind"));
    expect(screen.getByRole("listbox", { name: "Kind" })).toBeTruthy();
    await userEvent.click(target);
    await settleAll();
    expect(screen.queryByRole("listbox", { name: "Kind" })).toBeNull();
    expect(searchParam("type")).toBe("bug");
    expect(picker("Kind").textContent).toBe("Bug");
    expect(document.activeElement).toBe(picker("Kind"));
  }
});

test("after a right-click on Search closes Kind, Enter reopens it with the arrows still moving through its options", async () => {
  await mount("#/scouts", scoutFetch(demo, writer));
  await click(picker("Kind"));
  await userEvent.pointer({ keys: "[MouseRight]", target: field() });
  await settleAll();
  expect(screen.queryByRole("listbox", { name: "Kind" })).toBeNull();
  expect(document.activeElement).toBe(picker("Kind"));
  await userEvent.keyboard("{Enter}");
  await settleAll();
  expect(screen.getByRole("listbox", { name: "Kind" })).toBeTruthy();
  await key(document.activeElement!, "ArrowDown");
  expect(document.activeElement!.textContent).toBe("Bug");
});
