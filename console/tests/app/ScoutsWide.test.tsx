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
import { DEMO, fleet, MINUTE, NOW, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const servedRow = servedScouts.scouts[1]!;
const [servedTask] = served.tasks;
const servedAction = served.actions[0]!;

const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const ASK = "Why do loop runs crash with database is locked?";
const TITLE = "Loop runs crash with database is locked";

const row = (id: number, fields: Partial<ScoutRow> = {}): ScoutRow => ({ ...servedRow, id, ...fields });
const listed = [row(31), row(30), row(29)];

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
const other: ScoutDetail = { ...done, scout: { ...done.scout, id: 29, ask: "Which writer holds the lock?" } };

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
const pane = () => document.querySelector("[data-scout-pane]") as HTMLElement;
const searchParam = (name: string) => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get(name);

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
    matches: query === "(min-width: 1400px)" && wide,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as MediaQueryList);
});
afterEach(() => {
  cleanup();
  media?.mockRestore();
  window.location.hash = "";
});

test("a wide Scouts page shows the first scout in a pane under its title, its ask folded, with Action items, Findings and its hypotheses line, Progress and Thread", async () => {
  await mount("#/scouts");
  const region = screen.getByRole("region", { name: "scout 31" });
  expect(pane().contains(region)).toBe(true);
  expect(within(region).getByRole("heading", { level: 2, name: TITLE })).toBeTruthy();
  expect(within(region).getByRole("button", { name: "Ask", expanded: false })).toBeTruthy();
  expect(within(region).queryByText(ASK)).toBeNull();
  const items = within(region).getByRole("region", { name: "Action items" });
  expect(items.querySelector("[data-to-file]")!.textContent).toBe("2 to file");
  expect(current()).toEqual([["31", "true"]]);
  const findings = within(region).getByRole("region", { name: "Findings" });
  expect(items.compareDocumentPosition(findings) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(findings.querySelector("button[data-reproduction]")!.textContent).toBe("reproduced in a test · 1 hypothesis · 1 supported");
  const progress = within(region).getByRole("region", { name: "Progress" });
  expect(findings.compareDocumentPosition(progress) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(within(region).getByRole("textbox", { name: "Question for the scout" })).toBeTruthy();
  const facts = region.querySelector("[data-scout-facts]")!;
  expect(facts.querySelector("[data-scout-what]")!.textContent).toBe("scout 31 · bug · demo · base a41c9e2 · started Thu 11:46 PDT");
  expect(facts.querySelector("[data-scout-cost]")!.textContent).toBe("14m of 30m · lead opus · helpers luna ×4 · 10 of 10 back");
  expect(within(region).getByRole("link", { name: "Open scout 31" }).getAttribute("href")).toBe("/#/scouts/demo/31");
});

test("pressing a row selects it in the search, and a fresh mount at that address shows the same scout", async () => {
  await mount("#/scouts");
  await click(within(rowOf(29) as HTMLElement).getByRole("link"));
  expect(searchParam("scout")).toBe("demo/29");
  expect(current()).toEqual([["29", "true"]]);
  expect(screen.getByRole("region", { name: "scout 29" })).toBeTruthy();
  const hash = window.location.hash;
  cleanup();
  await mount(hash);
  expect(screen.getByRole("region", { name: "scout 29" })).toBeTruthy();
  expect(current()).toEqual([["29", "true"]]);
});

test("each wide row reads its kind, time taken and start, never its project, then what it found", async () => {
  const counts = { findings: 0, actions: 0, filed: 0, to_file: 0 };
  const rows = [
    row(31, { phase: "done", kind: "bug", elapsed_ms: 13 * MINUTE, started_ms: new Date(2026, 9, 24, 2, 28).getTime(), findings: 7, actions: 4, filed: 1, to_file: 3 }),
    row(30, { phase: "done", ...counts, findings: 11 }),
    row(29, { phase: "stopped", ...counts, findings: 2, actions: 2, to_file: 2 }),
    row(28, { phase: "swarming", ...counts, tasks_done: 5, tasks_total: 10 }),
    row(27, { phase: "done", ...counts, actions: 2, filed: 1, to_file: 1, tasks_done: 10, tasks_total: 10 }),
  ].map((each) => ({ ...each, lead: "opus", helper: "luna" }));
  const facts = (id: number) => rowOf(id).querySelector("[data-scout-row-facts]")!.textContent;
  const outcome = (id: number) => rowOf(id).querySelector("[data-scout-outcome]")!.textContent;
  await mount("#/scouts", wideFetch(rows));
  expect(facts(31)).toBe("bug · 13m · Sat 02:28 PDT");
  expect(outcome(31)).toBe("7 findings · 4 actions · 1 filed · 3 to file");
  expect(rowOf(31).querySelector("[data-to-file]")!.textContent).toBe("3 to file");
  expect(outcome(30)).toBe("11 findings");
  expect(outcome(29)).toBe("2 findings · 2 actions");
  expect(rowOf(29).querySelector("[data-to-file]")).toBeNull();
  expect(outcome(28)).toBe("5 of 10 back");
  expect(outcome(27)).toBe("2 actions · 1 filed · 1 to file");
  expect(list().textContent).not.toMatch(/opus|luna/);
  expect(screen.queryByText("Helpers")).toBeNull();
  expect(screen.queryByText("Took")).toBeNull();
  cleanup();
  await mount("#/scouts?project=demo", wideFetch(rows));
  expect(facts(31)).toBe("bug · 13m · Sat 02:28 PDT");
});

test("New scout opens the form in the pane over the same list with no row current, and Cancel brings the scout back", async () => {
  await mount("#/scouts");
  const before = list();
  await click(screen.getByRole("button", { name: "New scout" }));
  const form = screen.getByRole("form", { name: "New scout" });
  expect(pane().contains(form)).toBe(true);
  expect(list()).toBe(before);
  expect([...list().querySelectorAll("[data-scout]")].map((li) => li.getAttribute("data-scout"))).toEqual(["31", "30", "29"]);
  expect(list().querySelectorAll("[aria-current]").length).toBe(0);
  expect(screen.queryByRole("region", { name: "scout 31" })).toBeNull();
  await click(within(form).getByRole("button", { name: "Cancel" }));
  expect(pane().contains(screen.getByRole("region", { name: "scout 31" }))).toBe(true);
  expect(current()).toEqual([["31", "true"]]);
});

test("a Scout button's address opens the prefilled form in the pane", async () => {
  await mount("#/scouts?new=demo&kind=bug&ask=ASK");
  const form = screen.getByRole("form", { name: "New scout" });
  expect(pane().contains(form)).toBe(true);
  expect((within(form).getByLabelText("Ask") as HTMLTextAreaElement).value).toBe("ASK");
  const pressed = within(within(form).getByRole("group", { name: "Kind" })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true");
  expect(pressed.map((button) => button.textContent)).toEqual(["Bug"]);
});

test("below 1400px the page keeps its table, row links and form above the list, ignoring the scout search", async () => {
  wide = false;
  await mount("#/scouts?scout=demo/29");
  expect(screen.queryByRole("region", { name: "scout 29" })).toBeNull();
  expect(screen.queryByRole("region", { name: "scout 31" })).toBeNull();
  expect(pane()).toBeNull();
  expect(within(rowOf(31) as HTMLElement).getByRole("link").getAttribute("href")).toBe("/#/scouts/demo/31");
  await click(screen.getByRole("button", { name: "New scout" }));
  const form = screen.getByRole("form", { name: "New scout" });
  expect(form.compareDocumentPosition(list()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});
