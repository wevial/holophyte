import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, Status, TicketDetail, TicketSteer } from "../../src/lib/contract.gen";
import { ACTIONS_OFF } from "../../src/lib/actions";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, run, SERVE } from "./needsFleet";

const TOKEN = "tok-355";
const NOW = new Date(2026, 9, 15, 17, 0).getTime();
const at = (day: number, hour: number, minute: number) => new Date(2026, 9, day, hour, minute).getTime();

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const servedSteer = servedTicket.steers[0]!;

const steer = (facts: Partial<TicketSteer>): TicketSteer => ({ ...servedSteer, ...facts });
const STEERS = [
  steer({ id: 1, kind: "amendment", note: "Keep the estimate under an hour", author: "maintainer", at: at(14, 15, 2), run: null, read_by: null, read_ms: null }),
  steer({ id: 2, kind: "hint", note: "Reuse the sweep's lease query", author: "maintainer", at: at(15, 16, 40), run: 9, read_by: 9, read_ms: at(15, 16, 41) }),
];

const detail = (key: string, facts: Partial<TicketDetail>): TicketDetail => ({ ...servedTicket, ticket: key, title: `${key} title`, runs: [], steers: [], ...facts });
const tickets: Record<string, TicketDetail> = {
  "DEMO-3": detail("DEMO-3", { run: 9, status: "in_flight", steers: STEERS }),
  "DEMO-2": detail("DEMO-2", { run: null, status: "ready" }),
  "DEMO-10": detail("DEMO-10", { run: null, status: "merged" }),
};
const board: Board = {
  ...pinned,
  stories: [],
  columns: [{ state: "in_flight", tickets: [{ ...template, ticket: "DEMO-3", title: "DEMO-3 title", time_box_ms: 25 * MINUTE, run: 9 } as BoardTicket]}],
};

let requested: string[] = [];

/** DEMO runs DEMO-3 as run 9, with `actions` on its `/status` as given, or
 *  absent for null, and every `POST /actions/...` answered by `act`. */
async function mount(key: string, { actions = true, act: answer }: { actions?: boolean | null; act?: Fetch } = {}) {
  window.location.hash = `#/tickets/demo/${key}`;
  const polls = fleet({ items: [], runs: [{ ...run(9, "DEMO-3"), phase: "working" }] }, { items: [] });
  requested = [];
  const fake = fakeDeps(async (url, init) => {
    requested.push(url);
    if (url.includes("/actions/") && answer != null) return answer(url, init);
    if (url === `${DEMO}/status`) {
      const status = (await (await polls(url)).json()) as Status;
      const { actions: _served, ...rest } = status;
      return Response.json(actions == null ? rest : { ...rest, actions });
    }
    if (url === `${DEMO}/board`) return Response.json(board);
    const ticket = url.startsWith(`${DEMO}/tickets/`) ? decodeURIComponent(url.slice(`${DEMO}/tickets/`.length)) : null;
    if (ticket != null) return ticket in tickets ? Response.json(tickets[ticket]) : new Response("not found", { status: 404 });
    if (url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  });
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const steerButton = () => screen.getByRole("button", { name: "Steer" });
const box = () => screen.getByRole("region", { name: "Steer · amend this ticket" });
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const ticketReads = (key: string) => requested.filter((url) => url === `${DEMO}/tickets/${key}`).length;

async function type(note: string) {
  await act(async () => void fireEvent.change(within(box()).getByRole("textbox", { name: "Steer note" }), { target: { value: note } }));
}
async function press(name: string, within_: HTMLElement = box()) {
  await act(async () => void fireEvent.click(within(within_).getByRole("button", { name })));
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
beforeEach(() => storeToken(addressOf(DEMO), TOKEN));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the steer notes list counts the ticket's notes newest first, each with its kind, run, time, author and what became of it", async () => {
  await mount("DEMO-3");
  const list = screen.getByRole("region", { name: "Steer notes and amendments 2" });
  const entries = within(list).getAllByRole("listitem");
  const parts = (entry: HTMLElement) => [entry.querySelector("[data-steer-head]")!.textContent, entry.querySelector("p")!.textContent, entry.querySelector("[data-steer-fate]")!.textContent];
  expect(parts(entries[0]!)).toEqual(["Steer · run 9 · today 16:40", "Reuse the sweep's lease query", "the agent read it at 16:41"]);
  expect(parts(entries[1]!)).toEqual(["Amendment · yesterday 15:02 · by maintainer", "Keep the estimate under an hour", "every run since reads it with the ticket"]);
});

test("This run sends a hint to the live run once with the bearer, toasts, closes the box and re-reads the ticket", async () => {
  const action = fakeFetch({ ok: true, detail: "DEMO-3 steered" });
  await mount("DEMO-3", { act: action.fetchImpl });
  await press("Steer", document.body);
  expect(steerButton().getAttribute("aria-expanded")).toBe("true");
  const theTicket = within(box()).getByRole("button", { name: "The ticket" });
  expect(theTicket.getAttribute("aria-pressed")).toBe("true");
  await press("This run");
  expect(within(box()).getByRole("button", { name: "This run" }).getAttribute("aria-pressed")).toBe("true");
  expect(within(box()).getByRole("button", { name: "Send to run 9" }).hasAttribute("disabled")).toBe(true);
  await type("reuse --act");
  const reads = ticketReads("DEMO-3");
  await press("Send to run 9");
  expect(action.seen.map(({ url, body, authorization }) => ({ url, body, authorization }))).toEqual([
    { url: `${DEMO}/actions/steer`, body: { ticket: "DEMO-3", note: "reuse --act", hint: true }, authorization: `Bearer ${TOKEN}` },
  ]);
  expect(toast()).toBe("Steer note sent to run 9.");
  expect(screen.queryByRole("region", { name: "Steer · amend this ticket" })).toBeNull();
  expect(ticketReads("DEMO-3")).toBeGreaterThan(reads);
});

test("a ticket with no live run offers only The ticket, and saving posts an amendment", async () => {
  const action = fakeFetch({ ok: true, detail: "DEMO-2 amended" });
  await mount("DEMO-2", { act: action.fetchImpl });
  await press("Steer", document.body);
  const group = within(box()).getByRole("group", { name: "What the note changes" });
  expect(within(group).getAllByRole("button").map((button) => button.textContent)).toEqual(["The ticket"]);
  await type("add a criterion");
  await press("Save amendment");
  expect(action.seen.map(({ body }) => body)).toEqual([{ ticket: "DEMO-2", note: "add a criterion", hint: false }]);
  expect(toast()).toBe("Amendment saved to DEMO-2.");
});

test("a refused steer keeps the box open with the typed note, shows the daemon's detail and toasts nothing", async () => {
  const action = fakeFetch({ ok: false, detail: "DEMO-3 is merged; nothing to steer" });
  await mount("DEMO-3", { act: action.fetchImpl });
  await press("Steer", document.body);
  await type("add a criterion");
  await press("Save amendment");
  expect(within(box()).getByRole("alert").textContent).toBe("DEMO-3 is merged; nothing to steer");
  expect((within(box()).getByRole("textbox", { name: "Steer note" }) as HTMLTextAreaElement).value).toBe("add a criterion");
  expect(toast()).toBe("");
});

test("Steer is disabled with ACTIONS_OFF on a host whose /status has no actions, and a merged ticket draws no Steer button", async () => {
  await mount("DEMO-3", { actions: null });
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-3 title");
  expect(steerButton().hasAttribute("disabled")).toBe(true);
  expect(steerButton().getAttribute("title")).toBe(ACTIONS_OFF);
  cleanup();
  await mount("DEMO-10");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-10 title");
  expect(screen.queryByRole("button", { name: "Steer" })).toBeNull();
});

test("a draft stays with its ticket: moving to another ticket's page closes the box and opens it there empty", async () => {
  const action = fakeFetch({ ok: true, detail: "DEMO-2 amended" });
  await mount("DEMO-3", { act: action.fetchImpl });
  await press("Steer", document.body);
  await type("reuse --act");
  await act(async () => {
    window.location.hash = "#/tickets/demo/DEMO-2";
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  for (let round = 0; round < 6; round += 1) await act(settle);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-2 title");
  expect(screen.queryByRole("region", { name: "Steer · amend this ticket" })).toBeNull();
  await press("Steer", document.body);
  expect((within(box()).getByRole("textbox", { name: "Steer note" }) as HTMLTextAreaElement).value).toBe("");
  expect(action.seen).toEqual([]);
});
