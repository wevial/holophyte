import { afterAll, afterEach, beforeAll, beforeEach, expect, spyOn, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ScoutAction, ScoutDetail, ScoutRow, Scouts, ScoutTask } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const servedRow = servedScouts.scouts[1]!;
const [servedTask] = served.tasks;
const servedAction = served.actions[0]!;

const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const ASK = "Why do loop runs crash with database is locked?";
const TITLE = "Loop runs crash with database is locked";
const OTHER_TITLE = "Which writer holds the lock";

const row = (id: number, fields: Partial<ScoutRow> = {}): ScoutRow => ({ ...servedRow, id, ...fields });
const listed = [row(31, { title: TITLE }), row(30), row(29, { title: OTHER_TITLE })];

const task = (index: number): ScoutTask => ({
  ...servedTask!, id: index, pass: 1, question: `Question ${index}`, phase: "done", route: "luna", tokens: null,
  started_ms: at(11, 47), ended_ms: at(11, 49), duration_ms: 120_000,
});
const action = (position: number, ticket: string | null): ScoutAction => ({
  ...servedAction, position, pass: 1, kind: "investigate", title: `Item ${position}`, ticket, filed_ms: ticket == null ? null : at(12, 0),
});

const done: ScoutDetail = {
  ...served,
  scout: {
    ...served.scout, id: 31, ask: ASK, title: TITLE, kind: "bug", phase: "done", lead: "opus", helper: "luna", swarm: 4, subject: null,
    base_sha: "a41c9e2b7d0f4c3e9a1b2c3d4e5f60718293a4b5", created_ms: at(11, 45), started_ms: at(11, 46), ended_ms: at(12, 0),
    budget: { ...served.scout.budget, minutes: 30 },
  },
  tasks: Array.from({ length: 10 }, (_, index) => task(index + 1)),
  actions: [action(1, "DEMO-40"), action(2, null), action(3, null)],
};
const other: ScoutDetail = { ...done, scout: { ...done.scout, id: 29, ask: "Which writer holds the lock?", title: OTHER_TITLE } };

let wide = true;
let media: ReturnType<typeof spyOn> | null = null;

/** DEMO answering `/scouts` with `rows` and `/scouts/N` with scouts 31 and 29, 404 for any other. */
function wideFetch(rows: ScoutRow[] = listed): Fetch {
  const polls = fleet({ items: [] }, { items: [] });
  return async (url) => {
    const id = new RegExp(`^${DEMO}/scouts/(\\d+)$`).exec(url)?.[1];
    if (id != null) {
      const detail = { 31: done, 29: other }[Number(id)];
      return detail ? Response.json(detail) : Response.json({ error: "no such scout" }, { status: 404 });
    }
    if (url === `${DEMO}/scouts`) return Response.json({ ...servedScouts, scouts: rows });
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: true });
  };
}

async function mount(hash: string, fetch: Fetch = wideFetch()) {
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

const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  for (let round = 0; round < 4; round += 1) await act(settle);
};

const list = () => screen.getByRole("list", { name: "Scouts, newest first" });
const rowOf = (id: number) => list().querySelector(`[data-scout="${id}"]`)!;
const current = () => [...list().querySelectorAll("li[aria-current]")].map((li) => [li.getAttribute("data-scout"), li.getAttribute("aria-current")]);
const sidebar = () => screen.queryByRole("complementary", { name: "Scout list" });
const page = () => document.querySelector("[data-scout-page]") as HTMLElement;
const path = () => window.location.hash.split("?")[0];

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
beforeEach(() => {
  wide = true;
  media = spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches: query === "(min-width: 1100px)" && wide,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as MediaQueryList);
});
afterEach(() => {
  cleanup();
  media?.mockRestore();
  window.location.hash = "";
  localStorage.clear();
});

test("a wide Scouts page shows the first scout's page beside the list with its row current, and no preview pane or Open scout anywhere", async () => {
  await mount("#/scouts");
  const region = screen.getByRole("region", { name: "scout 31" });
  expect(page().contains(region)).toBe(true);
  expect(sidebar()!.contains(region)).toBe(false);
  expect(within(region).getByRole("heading", { level: 1 }).textContent).toBe(TITLE);
  expect(current()).toEqual([["31", "page"]]);
  expect(document.querySelector("[data-scout-pane]")).toBeNull();
  expect(screen.queryByRole("link", { name: /Open scout/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /Open scout/ })).toBeNull();
  expect(screen.queryByText("Open scout")).toBeNull();
});

test("pressing the second row opens that scout's page at its own address beside the same list, its row marked current", async () => {
  await mount("#/scouts");
  await click(within(rowOf(29) as HTMLElement).getByRole("link"));
  expect(path()).toBe("#/scouts/demo/29");
  const region = screen.getByRole("region", { name: "scout 29" });
  expect(within(region).getByRole("heading", { level: 1 }).textContent).toBe(OTHER_TITLE);
  expect(sidebar()!.contains(list())).toBe(true);
  expect(sidebar()!.contains(region)).toBe(false);
  expect([...list().querySelectorAll("[data-scout]")].map((li) => li.getAttribute("data-scout"))).toEqual(["31", "30", "29"]);
  expect(current()).toEqual([["29", "page"]]);
});

test("hiding the list leaves a Scouts button at the top left of the scout's page that brings it back, and each choice survives a remount", async () => {
  await mount("#/scouts/demo/31");
  await click(screen.getByRole("button", { name: "Hide scout list", expanded: true }));
  expect(sidebar()).toBeNull();
  const show = screen.getByRole("button", { name: "Scouts", expanded: false });
  expect(document.activeElement).toBe(show);
  const rail = screen.getByRole("complementary", { name: "Summary and decisions" });
  expect(rail.contains(show)).toBe(true);
  expect(show.compareDocumentPosition(within(rail).getByRole("navigation", { name: "Breadcrumb" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  cleanup();
  await mount("#/scouts/demo/31");
  expect(sidebar()).toBeNull();
  await click(screen.getByRole("button", { name: "Scouts", expanded: false }));
  expect(sidebar()!.contains(list())).toBe(true);
  expect(screen.queryByRole("button", { name: "Scouts" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Hide scout list" }));
  cleanup();
  await mount("#/scouts/demo/31");
  expect(sidebar()!.contains(list())).toBe(true);
});

test("New scout opens the form in place of the scout beside the list with no row current, and Cancel brings the scout back", async () => {
  await mount("#/scouts");
  await click(screen.getByRole("button", { name: "New scout" }));
  const form = screen.getByRole("form", { name: "New scout" });
  expect(page().contains(form)).toBe(true);
  expect([...list().querySelectorAll("[data-scout]")].map((li) => li.getAttribute("data-scout"))).toEqual(["31", "30", "29"]);
  expect(list().querySelectorAll("[aria-current]").length).toBe(0);
  expect(screen.queryByRole("region", { name: "scout 31" })).toBeNull();
  await click(within(form).getByRole("button", { name: "Cancel" }));
  expect(page().contains(screen.getByRole("region", { name: "scout 31" }))).toBe(true);
  expect(current()).toEqual([["31", "page"]]);
});

test("New scout on a scout's page opens the form on the Scouts page for that scout's project", async () => {
  await mount("#/scouts/demo/29");
  await click(within(sidebar()!).getByRole("button", { name: "New scout" }));
  expect(path()).toBe("#/scouts");
  const form = screen.getByRole("form", { name: "New scout" });
  expect(page().contains(form)).toBe(true);
  expect(sidebar()!.contains(list())).toBe(true);
});

test("a Scout button's address opens the prefilled form beside the list", async () => {
  await mount("#/scouts?new=demo&kind=bug&ask=ASK");
  const form = screen.getByRole("form", { name: "New scout" });
  expect(page().contains(form)).toBe(true);
  expect((within(form).getByLabelText("Ask") as HTMLTextAreaElement).value).toBe("ASK");
  const pressed = within(within(form).getByRole("group", { name: "Kind" })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true");
  expect(pressed.map((button) => button.textContent)).toEqual(["Bug"]);
});

test("below 1100px the Scouts page keeps its table, row links and form above the list, with no scout shown", async () => {
  wide = false;
  await mount("#/scouts");
  expect(screen.queryByRole("region", { name: "scout 31" })).toBeNull();
  expect(sidebar()).toBeNull();
  expect(document.querySelector("[data-scout-table]")).not.toBeNull();
  expect(within(rowOf(31) as HTMLElement).getByRole("link").getAttribute("href")).toBe("/#/scouts/demo/31");
  await click(screen.getByRole("button", { name: "New scout" }));
  const form = screen.getByRole("form", { name: "New scout" });
  expect(form.compareDocumentPosition(list()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
