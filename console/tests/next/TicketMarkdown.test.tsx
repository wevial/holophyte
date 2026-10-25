import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, Status, TicketDetail } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const TOKEN = "tok-body";

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;

const SECTIONS = `# DEMO-2 title

## Summary

The whole body shows on the page.

## Out of scope

- The classic sheet.

## Implementation notes

- Read the header once.

## Open questions

- None
`;
const HEADED = `+++
ui_change = "minor"
estimate = 30
depends_on = []

[[evidence]]
file = "01-a.png"
state = "the first state"

[[evidence]]
file = "02-b.png"
state = "the second state"
+++
${SECTIONS}`;

const card = (facts: Partial<BoardTicket>) => ({ ...template, ticket: "DEMO-2", title: "DEMO-2 title", column: "ready", revision: 3, run: null, ...facts }) as BoardTicket;

let gets: string[] = [];
let puts: { url: string; ifMatch: string | null }[] = [];

/** DEMO-2 at revision 3 holding `body` behind a `/board` that is `editable`
 *  as given; every `PUT /tickets/DEMO-2` is answered by `write`. */
async function mount({ body = HEADED, editable = true, write }: { body?: string; editable?: boolean; write?: Fetch } = {}) {
  window.location.hash = "#/tickets/demo/DEMO-2";
  const ticket: TicketDetail = {
    ...servedTicket,
    ticket: "DEMO-2",
    title: "DEMO-2 title",
    run: null,
    status: "ready",
    runs: [],
    steers: [],
    body: SECTIONS,
    current: { ...servedTicket.current!, revision: 3, column: "ready", body },
  };
  const polls = fleet({ items: [], runs: [] }, { items: [] });
  gets = [];
  puts = [];
  const fake = fakeDeps(async (url, init) => {
    if (init?.method === "PUT" && write != null) {
      puts.push({ url, ifMatch: new Headers(init.headers).get("if-match") });
      return write(url, init);
    }
    gets.push(url);
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions: true });
    if (url === `${DEMO}/board`) return Response.json({ ...pinned, editable, stories: [], columns: [{ state: "ready", tickets: [card({})] }] });
    if (url === `${DEMO}/tickets/DEMO-2`) return Response.json(ticket);
    if (url.startsWith(`${DEMO}/tickets/`) || url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  });
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const viewGroup = () => screen.getByRole("group", { name: "Ticket view" });
const leftCard = () => document.querySelector<HTMLElement>("[data-ticket-body]")!;
const actions = () => document.querySelector<HTMLElement>("[data-ticket-actions]")!;
const editor = () => screen.queryByRole<HTMLTextAreaElement>("textbox", { name: "Ticket body" });
const saveButton = () => screen.getByRole("button", { name: "Save revision 4" });
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const ticketReads = () => gets.filter((url) => url === `${DEMO}/tickets/DEMO-2`).length;
const pressed = () => within(viewGroup()).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.textContent);
const headings = () => within(leftCard()).getAllByRole("heading").map((heading) => heading.textContent);

async function click(element: HTMLElement) {
  await act(async () => void fireEvent.click(element));
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function openEditor(edited = "") {
  await click(within(viewGroup()).getByRole("button", { name: "Markdown" }));
  await click(within(actions()).getByRole("button", { name: "Edit" }));
  if (edited) await act(async () => void fireEvent.change(editor()!, { target: { value: edited } }));
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
beforeEach(() => storeToken(addressOf(DEMO), TOKEN));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("Markdown draws the header as a facts table above every section, without the fence, the title or the Fields block", async () => {
  await mount();
  expect(pressed()).toEqual(["Fields"]);
  expect(within(leftCard()).getByText("Summary")).toBeTruthy();
  expect(within(leftCard()).getByRole("region", { name: "Acceptance criteria" })).toBeTruthy();
  await click(within(viewGroup()).getByRole("button", { name: "Markdown" }));
  expect(pressed()).toEqual(["Markdown"]);
  const facts = within(leftCard()).getByRole("table", { name: "Ticket header" });
  expect(within(facts).getAllByRole("row").map((row) => [...row.children].map((cell) => cell.textContent))).toEqual([
    ["ui_change", "minor"],
    ["estimate", "30"],
    ["depends_on", "none"],
    ["evidence", "01-a.png · 02-b.png"],
  ]);
  expect(headings()).toEqual(["Summary", "Out of scope", "Implementation notes", "Open questions"]);
  expect(leftCard().textContent).not.toContain("+++");
  expect(screen.getAllByRole("heading", { name: "DEMO-2 title" })).toHaveLength(1);
  expect(within(leftCard()).queryByRole("region", { name: "Acceptance criteria" })).toBeNull();
});

test("a body without a header renders its sections with no facts table, and a board that takes no writes draws no Edit", async () => {
  await mount({ body: SECTIONS, editable: false });
  await click(within(viewGroup()).getByRole("button", { name: "Markdown" }));
  expect(headings()).toEqual(["Summary", "Out of scope", "Implementation notes", "Open questions"]);
  expect(within(leftCard()).queryByRole("table")).toBeNull();
  expect(within(actions()).queryByRole("button", { name: "Edit" })).toBeNull();
  expect(within(actions()).queryByRole("link", { name: "Edit" })).toBeNull();
});

test("Edit under Markdown opens the served body raw at its revision in place; under Fields it stays the Edit page link", async () => {
  await mount();
  await click(within(viewGroup()).getByRole("button", { name: "Markdown" }));
  await click(within(viewGroup()).getByRole("button", { name: "Fields" }));
  expect(within(actions()).getByRole<HTMLAnchorElement>("link", { name: "Edit" }).hash).toBe("#/tickets/demo/DEMO-2/edit");
  await openEditor();
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-2");
  expect(editor()!.value).toBe(HEADED);
  expect(saveButton().hasAttribute("disabled")).toBe(true);
  expect(within(viewGroup()).getAllByRole("button").map((button) => button.hasAttribute("disabled"))).toEqual([true, true]);
  expect(within(actions()).getByRole("button", { name: "Edit" }).hasAttribute("disabled")).toBe(true);
});

test("Save puts the edited body alone at the opened revision with the bearer, toasts, re-reads the ticket and returns to Markdown", async () => {
  const write = fakeFetch({ ticket: "DEMO-2", revision: 4 });
  await mount({ write: write.fetchImpl });
  const edited = HEADED.replace("- None", "- Which font?");
  await openEditor(edited);
  const reads = ticketReads();
  await click(saveButton());
  expect(write.seen.map(({ url, method, authorization, body }) => ({ url, method, authorization, body }))).toEqual([
    { url: `${DEMO}/tickets/DEMO-2`, method: "PUT", authorization: `Bearer ${TOKEN}`, body: { body: edited } },
  ]);
  expect(puts.map((put) => put.ifMatch)).toEqual(["3"]);
  expect(toast()).toBe("Saved DEMO-2 as revision 4.");
  expect(editor()).toBeNull();
  expect(pressed()).toEqual(["Markdown"]);
  expect(ticketReads()).toBeGreaterThan(reads);
});

test("a Save answered 409 keeps the text, holds Save and re-reads; one answered 422 lists its problems and keeps the text", async () => {
  const moved = fakeFetch(() => Response.json({ error: "ticket DEMO-2 moved from revision 3 to 4 since it was admitted", current: 4 }, { status: 409 }));
  await mount({ write: moved.fetchImpl });
  const edited = `${HEADED}\nMore.\n`;
  await openEditor(edited);
  const reads = ticketReads();
  await click(saveButton());
  expect(within(leftCard()).getByRole("alert").textContent).toBe("DEMO-2 changed since this page read it: revision 4 is current. Your text is kept.");
  expect(editor()!.value).toBe(edited);
  expect(saveButton().hasAttribute("disabled")).toBe(true);
  expect(toast()).toBe("");
  expect(ticketReads()).toBeGreaterThan(reads);
  cleanup();
  const refused = fakeFetch(() => Response.json({ problems: ["missing section '## Summary'", "no acceptance criteria"] }, { status: 422 }));
  await mount({ write: refused.fetchImpl });
  await openEditor(edited);
  await click(saveButton());
  expect([...within(leftCard()).getByRole("alert").children].map((line) => line.textContent)).toEqual(["missing section '## Summary'", "no acceptance criteria"]);
  expect(editor()!.value).toBe(edited);
});

test("Discard closes the editor without sending anything and shows the served text", async () => {
  const write = fakeFetch({ ticket: "DEMO-2", revision: 4 });
  await mount({ write: write.fetchImpl });
  await openEditor(HEADED.replace("## Out of scope", "## Dropped"));
  await click(within(leftCard()).getByRole("button", { name: "Discard" }));
  expect(write.seen).toEqual([]);
  expect(editor()).toBeNull();
  expect(headings()).toEqual(["Summary", "Out of scope", "Implementation notes", "Open questions"]);
});
