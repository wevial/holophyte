import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { ScoutRoutes, ScoutRow, Scouts } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, SERVE, WRITER } from "./needsFleet";

const servedScouts = (await Bun.file(new URL("scouts.json", SERVE)).json()) as Scouts;
const servedRoutes = (await Bun.file(new URL("scout-routes.json", SERVE)).json()) as ScoutRoutes;
const swarming = servedScouts.scouts[1]!;

const scout = (id: number, phase: ScoutRow["phase"], minutesAgo: number, row: Partial<ScoutRow> = {}): ScoutRow => ({
  ...swarming, id, phase, created_ms: NOW - minutesAgo * MINUTE, ...row,
});

const lead = servedRoutes.leads[0]!;
const helper = servedRoutes.helpers[0]!;
const demoRoutes: ScoutRoutes = {
  leads: [{ ...lead, name: "opus" }, { ...lead, name: "sol", default: false, probe: null }],
  helpers: [{ ...helper, name: "luna" }],
  defaults: { ...servedRoutes.defaults, lead: "opus", helper: "luna", swarm: 4, minutes: 30, helper_turns: 4 },
};
const writerRoutes: ScoutRoutes = {
  leads: [{ ...lead, name: "glm" }],
  helpers: [{ ...helper, name: "haiku" }],
  defaults: { ...servedRoutes.defaults, lead: "glm", helper: "haiku", swarm: 2, minutes: 15, helper_turns: 6 },
};

interface Daemons {
  demo: ScoutRow[];
  writer?: ScoutRow[];
  routes?: ScoutRoutes;
  actions?: boolean;
  launch?: () => Response;
}

/** DEMO and WRITER with `/scouts` and `/scouts/routes` answered from
 *  `daemons`; WRITER without scouts answers 404. Every request is kept. */
function scoutFetch(daemons: Daemons) {
  const seen: { url: string; method: string; body: unknown }[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fetch: Fetch = async (url, init) => {
    seen.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    if (url === `${DEMO}/scouts`) return Response.json({ ...servedScouts, scouts: daemons.demo });
    if (url === `${WRITER}/scouts` && daemons.writer) return Response.json({ ...servedScouts, scouts: daemons.writer });
    if (url === `${DEMO}/scouts/routes`) return Response.json(daemons.routes ?? demoRoutes);
    if (url === `${WRITER}/scouts/routes`) return Response.json(writerRoutes);
    if (url === `${DEMO}/actions/scout` && daemons.launch) return daemons.launch();
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: daemons.actions ?? true });
  };
  return { fetch, seen };
}

async function mount(hash: string, fetch: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  for (let round = 0; round < 4; round += 1) await act(settle);
};
const change = async (element: Element, value: string) => {
  await act(async () => void fireEvent.change(element, { target: { value } }));
  for (let round = 0; round < 4; round += 1) await act(settle);
};

const pages = () => within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("link").filter((link) => !link.closest("ul"));
const scoutsLink = () => pages().at(-1)!;
const list = () => screen.getByRole("list", { name: "Scouts, newest first" });
const ids = () => within(list()).getAllByRole("listitem").map((row) => row.getAttribute("data-scout"));
const countLine = () => document.querySelector("[data-count-line]")!.textContent;
const form = () => screen.getByRole("form", { name: "New scout" });
const pressed = (group: string) =>
  within(within(form()).getByRole("group", { name: group })).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent);
const buttons = (group: string) => within(within(form()).getByRole("group", { name: group })).getAllByRole("button").map((button) => button.textContent);
const disclosure = () => form().querySelector("button[aria-expanded]")!;
const scoutButton = () => within(form()).getByRole("button", { name: "Scout" });

function readRow(id: number) {
  const row = list().querySelector(`[data-scout="${id}"]`)!;
  const cell = (name: string) => row.querySelector(`[data-cell="${name}"]`)!.textContent;
  return {
    state: within(row as HTMLElement).getByRole("img").getAttribute("aria-label"),
    id: cell("id"), ask: row.querySelector("[data-cell='ask']")!.firstChild!.textContent, kind: cell("kind"),
    project: cell("project"), lead: cell("lead"), helpers: cell("helpers"), took: cell("took"),
    reason: row.querySelector("[data-reason]")?.textContent ?? null, text: row.textContent,
  };
}

async function openForm(hash: string, daemons: Daemons) {
  const scouts = scoutFetch(daemons);
  await mount(hash, scouts.fetch);
  await click(screen.getByRole("button", { name: "New scout" }));
  return scouts;
}

/** The form filled as a reader would: an ask, Bug, sol, two at once, ten minutes. */
async function fillForm(ask: string) {
  await change(within(form()).getByLabelText("Ask"), ask);
  await click(within(within(form()).getByRole("group", { name: "Kind" })).getByRole("button", { name: "Bug" }));
  await click(disclosure());
  await click(within(within(form()).getByRole("group", { name: "Lead" })).getByRole("button", { name: "sol" }));
  await change(within(form()).getByLabelText("At once"), "2");
  await change(within(form()).getByLabelText("Minutes"), "10");
}

const ASK = "How should the console stream run events?";
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the Scouts page link follows Findings, is current on #/scouts and counts running scouts", async () => {
  await mount("#/scouts", scoutFetch({ demo: [scout(31, "swarming", 1), scout(30, "planning", 2), scout(28, "done", 3)] }).fetch);
  expect(pages().map((link) => link.getAttribute("aria-label"))).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts, 2 running"]);
  expect(scoutsLink().getAttribute("aria-current")).toBe("page");
  expect(scoutsLink().querySelector("[data-badge]")?.textContent).toBe("2");
  cleanup();
  await mount("#/scouts", scoutFetch({ demo: [scout(28, "done", 3)] }).fetch);
  expect(scoutsLink().getAttribute("aria-label")).toBe("Scouts");
  expect(scoutsLink().querySelector("[data-badge]")).toBeNull();
});

test("the list merges every scoped project's scouts newest first under its count line", async () => {
  const daemons = { demo: [scout(31, "swarming", 1), scout(29, "queued", 3), scout(27, "done", 4)], writer: [scout(30, "planning", 2)] };
  await mount("#/scouts", scoutFetch(daemons).fetch);
  expect(ids()).toEqual(["31", "30", "29", "27"]);
  expect(countLine()).toBe("all projects · 4 scouts / 2 running / 1 queued / newest first");
  cleanup();
  await mount("#/scouts?project=demo", scoutFetch(daemons).fetch);
  expect(ids()).toEqual(["31", "29", "27"]);
  expect(countLine()).toStartWith("demo · 3 scouts");
});

test("a row reads its state, id, ask, kind, project, routes, helper progress and time taken", async () => {
  const running = scout(31, "swarming", 1, {
    ask: "Why do loop runs crash with database is locked?", kind: "bug", lead: "opus", helper: "luna",
    tasks_done: 5, tasks_total: 10, elapsed_ms: 14 * MINUTE,
  });
  const queued = scout(29, "queued", 2, { elapsed_ms: null, started_ms: null, tasks_done: 0, tasks_total: 0 });
  const failed = scout(25, "failed", 3, { reason: "lead route opus is down: probe timed out\nmore" });
  const ended = [scout(24, "done", 4), scout(23, "stopped", 5)];
  await mount("#/scouts?project=demo", scoutFetch({ demo: [running, queued, failed, ...ended] }).fetch);
  expect(readRow(31)).toMatchObject({
    state: "running", id: "31", ask: "Why do loop runs crash with database is locked?", kind: "bug",
    project: "demo", lead: "opus", helpers: "luna 5 of 10", took: "14m",
  });
  expect(readRow(29)).toMatchObject({ state: "queued", took: "–" });
  const fail = readRow(25);
  expect(fail).toMatchObject({ state: "failed", reason: "lead route opus is down: probe timed out" });
  expect(fail.text).not.toContain("more");
  expect([readRow(24).state, readRow(23).state]).toEqual(["done", "stopped"]);
});

test("with no scouts the page says so for its scope", async () => {
  await mount("#/scouts?project=demo", scoutFetch({ demo: [] }).fetch);
  expect(screen.getByText("No scouts in demo.")).toBeTruthy();
});

test("New scout opens the form on Ask with Research, the scoped project and the default routes picked", async () => {
  await openForm("#/scouts?project=demo", { demo: [] });
  expect(document.activeElement).toBe(within(form()).getByLabelText("Ask"));
  expect(pressed("Kind")).toEqual(["Research"]);
  const project = within(form()).getByLabelText("Project") as HTMLSelectElement;
  expect(project.selectedOptions[0]!.textContent).toBe("demo");
  const fold = within(form()).getByRole("button", { name: "opus · luna ×4 · 30 min" });
  expect(fold.getAttribute("aria-expanded")).toBe("false");
  await click(fold);
  expect(buttons("Lead")).toEqual(["opus", "sol"]);
  expect(pressed("Lead")).toEqual(["opus"]);
  expect(buttons("Helpers")).toEqual(["luna"]);
});

test("Cancel closes the form and New scout opens it again as typed", async () => {
  await openForm("#/scouts?project=demo", { demo: [] });
  await fillForm(ASK);
  await click(within(form()).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("form", { name: "New scout" })).toBeNull();
  await click(screen.getByRole("button", { name: "New scout" }));
  expect((within(form()).getByLabelText("Ask") as HTMLTextAreaElement).value).toBe(ASK);
  expect(pressed("Kind")).toEqual(["Bug"]);
  expect(disclosure().textContent).toBe("sol · luna ×2 · 10 min");
});

test("defaults naming unlisted routes press the first listed, and a project with no lead cannot scout", async () => {
  await openForm("#/scouts?project=demo", { demo: [], routes: { ...demoRoutes, defaults: { ...demoRoutes.defaults, lead: "glm", helper: "haiku" } } });
  await click(within(form()).getByRole("button", { name: "opus · luna ×4 · 30 min" }));
  expect(pressed("Lead")).toEqual(["opus"]);
  expect(pressed("Helpers")).toEqual(["luna"]);
  cleanup();
  await openForm("#/scouts?project=demo", { demo: [], routes: { ...demoRoutes, leads: [] } });
  await change(within(form()).getByLabelText("Ask"), ASK);
  expect(scoutButton().hasAttribute("disabled")).toBe(true);
  expect(within(form()).getByText("No lead route on demo passed its probe.")).toBeTruthy();
});

test("the disclosure label follows the picks, and a blank ask or too large a swarm disables Scout", async () => {
  await openForm("#/scouts?project=demo", { demo: [] });
  await fillForm(ASK);
  expect(disclosure().textContent).toBe("sol · luna ×2 · 10 min");
  expect(scoutButton().hasAttribute("disabled")).toBe(false);
  await change(within(form()).getByLabelText("At once"), "5");
  expect(scoutButton().hasAttribute("disabled")).toBe(true);
  await change(within(form()).getByLabelText("At once"), "2");
  await change(within(form()).getByLabelText("Ask"), "  ");
  expect(scoutButton().hasAttribute("disabled")).toBe(true);
});

test("Scout posts the form once, closes it, says the daemon's detail and reads the list again", async () => {
  const launch = () => Response.json({ ok: true, detail: "scout 32 queued: 2 of 2 scout slots in use" });
  const { seen } = await openForm("#/scouts?project=demo", { demo: [], launch });
  await fillForm(ASK);
  const listReads = () => seen.filter((request) => request.url === `${DEMO}/scouts`).length;
  const before = listReads();
  await click(scoutButton());
  const posts = seen.filter((request) => request.method === "POST");
  expect(posts.map((post) => post.url)).toEqual([`${DEMO}/actions/scout`]);
  expect(posts[0]!.body).toEqual({ ask: ASK, kind: "bug", lead: "sol", helper: "luna", swarm: 2, minutes: 10 });
  expect(screen.queryByRole("form", { name: "New scout" })).toBeNull();
  expect(document.querySelector("[data-toast]")!.textContent).toBe("Scout 32 queued: 2 of 2 scout slots in use.");
  expect(listReads()).toBeGreaterThan(before);
});

test("a refusal keeps the form open as typed and reads under its buttons", async () => {
  const refusals = [
    { launch: () => Response.json({ ok: false, detail: "lead route glm is not in [scout.leads]" }), says: "lead route glm is not in [scout.leads]" },
    {
      launch: () => Response.json({ error: "kind must be bug, research or recon" }, { status: 400 }),
      says: `${DEMO}/actions/scout answered 400: kind must be bug, research or recon`,
    },
  ];
  for (const { launch, says } of refusals) {
    await openForm("#/scouts?project=demo", { demo: [], launch });
    await fillForm(ASK);
    await click(scoutButton());
    expect((within(form()).getByLabelText("Ask") as HTMLTextAreaElement).value).toBe(ASK);
    expect(within(form()).getByRole("alert").textContent).toBe(says);
    cleanup();
  }
});

test("with no scope the Project select lists every project taking actions and reads the picked one's routes", async () => {
  const { seen } = await openForm("#/scouts", { demo: [] });
  const project = within(form()).getByLabelText("Project") as HTMLSelectElement;
  expect(Array.from(project.options, (option) => option.textContent)).toEqual(["demo", "writer"]);
  await change(project, WRITER);
  expect(seen.some((request) => request.url === `${WRITER}/scouts/routes`)).toBe(true);
  expect(within(form()).getByRole("button", { name: "glm · haiku ×2 · 15 min" })).toBeTruthy();
});

test("with no project taking actions New scout is disabled under the actions-off title", async () => {
  await mount("#/scouts", scoutFetch({ demo: [], actions: false }).fetch);
  const button = screen.getByRole("button", { name: "New scout" });
  expect(button.hasAttribute("disabled")).toBe(true);
  expect(button.getAttribute("title")).toBe(ACTIONS_OFF);
});
