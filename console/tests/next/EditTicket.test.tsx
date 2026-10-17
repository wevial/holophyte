import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, Status, TicketCheck, TicketDetail } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch, PollDeps } from "../../src/lib/poll";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, SERVE } from "./needsFleet";

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;

const AC2 = "A note's author shows beside it.";
const BODY = `# Steer notes show on the run page

## Summary

The run page lists each steer note the operator left.

## What / Why / How

**What:** List the notes under the run.

**Why:** Operators lose track of what they asked.

**How:** Read the stored steers.

## In scope

- The run page

## Out of scope

- Notifications

## Acceptance criteria

- [ ] Each note shows on the run page.
  Its author and time show beside it.
- [ ] ${AC2}
- [ ] A withdrawn note shows struck through.

## Verify command(s)

\`\`\`
bun --cwd=console test
\`\`\`

## Evidence

- 01-notes.png: the run page with two notes.

## Estimate & dependencies

Estimate: 30 min · Depends on: none

## Open questions

- None
`;

const demo2 = (facts: Partial<TicketDetail> = {}): TicketDetail => ({
  ...servedTicket,
  ticket: "DEMO-2",
  title: "Steer notes show on the run page",
  status: "ready",
  run: null,
  runs: [],
  steers: [],
  body: BODY,
  current: { ...servedTicket.current!, revision: 3, at: NOW - 120 * MINUTE, author: "console", body: BODY, priority: 2, column: "ready" },
  ...facts,
});
const board = (editable: boolean): Board => ({
  ...pinned,
  editable,
  stories: [],
  columns: [{ state: "ready", tickets: [{ ...template, ticket: "DEMO-2", title: "Steer notes show on the run page", column: "ready", revision: 3, run: null } as BoardTicket]}],
});

let puts: { ifMatch: string | null; body: unknown }[] = [];
let checks: unknown[] = [];

/** Fires each captured timer that has fallen due on a clock moved by `ms`. */
let advance: (ms: number) => Promise<void>;

async function flush() {
  for (let round = 0; round < 6; round += 1) await act(settle);
}

/** DEMO serving DEMO-2 as `ticket` behind a `/board` that is `editable`;
 *  `/tickets/check` is answered by `check`, `PUT /tickets/DEMO-2` by `save`. */
async function mount(hash: string, { editable = true, ticket = demo2(), check = { problems: [], advisories: [], blocks: false }, save }: { editable?: boolean; ticket?: TicketDetail; check?: TicketCheck | (() => TicketCheck); save?: Fetch } = {}) {
  window.location.hash = hash;
  const polls = fleet({ items: [] }, { items: [] });
  puts = [];
  checks = [];
  const fake = fakeDeps(async (url, init) => {
    if (url === `${DEMO}/tickets/check`) {
      checks.push(JSON.parse(String(init?.body)));
      return Response.json(typeof check === "function" ? check() : check);
    }
    if (init?.method === "PUT") {
      puts.push({ ifMatch: new Headers(init.headers).get("if-match"), body: JSON.parse(String(init.body)) });
      return save!(url, init);
    }
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions: true });
    if (url === `${DEMO}/board`) return Response.json(board(editable));
    if (url === `${DEMO}/tickets/DEMO-2`) return Response.json(ticket);
    if (url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  });
  let now = 0;
  const timers: { at: number; fn: () => void }[] = [];
  const deps: PollDeps = {
    ...fake.deps,
    now: () => NOW,
    timer: (fn, ms) => {
      const entry = { at: now + ms, fn };
      timers.push(entry);
      return () => {
        const at = timers.indexOf(entry);
        if (at >= 0) timers.splice(at, 1);
      };
    },
  };
  advance = async (ms) => {
    now += ms;
    for (const entry of timers.filter((timer) => timer.at <= now)) {
      const at = timers.indexOf(entry);
      if (at < 0) continue;
      timers.splice(at, 1);
      await act(async () => entry.fn());
    }
    await flush();
  };
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={deps} />
    </QueryClientProvider>,
  );
  await flush();
}

const box = (name: string) => screen.getByRole("textbox", { name }) as HTMLInputElement;
const saveButton = () => screen.getByRole("button", { name: /^Save revision|^Saving/ }) as HTMLButtonElement;
const savePanel = () => screen.getByRole("complementary", { name: "Save revision" });
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const SERVED = { Title: "Steer notes show on the run page", AC1: "Each note shows on the run page.", AC2, AC3: "A withdrawn note shows struck through.", Summary: "The run page lists each steer note the operator left." };

async function type(name: string, value: string) {
  await act(async () => void fireEvent.change(box(name), { target: { value } }));
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
beforeEach(() => storeToken(addressOf(DEMO), "tok-410"));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the Ticket page links Edit to the edit page only while its board is editable and the ticket is not merged", async () => {
  await mount("#/tickets/demo/DEMO-2");
  const actions = document.querySelector<HTMLElement>("[data-ticket-actions]")!;
  expect(within(actions).getByRole<HTMLAnchorElement>("link", { name: "Edit" }).hash).toBe("#/tickets/demo/DEMO-2/edit");
  cleanup();
  await mount("#/tickets/demo/DEMO-2", { editable: false });
  expect(screen.queryByRole("link", { name: "Edit" })).toBeNull();
  cleanup();
  await mount("#/tickets/demo/DEMO-2", { ticket: demo2({ status: "merged" }) });
  expect(screen.queryByRole("link", { name: "Edit" })).toBeNull();
});

test("the edit page loads the current revision with its header line and holds Save until a field changes", async () => {
  await mount("#/tickets/demo/DEMO-2/edit");
  expect(screen.getByRole("navigation", { name: "Breadcrumb" }).textContent).toBe("Tickets / demo / DEMO-2 / edit");
  expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByRole<HTMLAnchorElement>("link", { name: "DEMO-2" }).hash).toBe("#/tickets/demo/DEMO-2");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Edit DEMO-2");
  expect(screen.getByText("revision 3 · saved 2h ago by console · Ready")).toBeTruthy();
  for (const [name, value] of Object.entries(SERVED)) expect(box(name).value).toBe(value);
  expect(saveButton().textContent).toBe("Save revision 4");
  expect(saveButton().disabled).toBe(true);
  expect(within(savePanel()).getByText("No changes yet.")).toBeTruthy();
});

test("Save sends the served body with only the rewritten criterion at the loaded revision, toasts and opens the Ticket page", async () => {
  const save = fakeFetch({ ticket: "DEMO-2", revision: 4 });
  await mount("#/tickets/demo/DEMO-2/edit", { save: save.fetchImpl });
  const rewritten = "A note the agent skipped shows skipped and why.";
  await type("AC2", rewritten);
  expect(box("AC2").closest("[data-changed]")).not.toBeNull();
  expect(box("AC1").closest("[data-changed]")).toBeNull();
  expect(within(savePanel()).getByText("1 change · Acceptance criteria · the next run of DEMO-2 reads the new text")).toBeTruthy();
  await act(async () => void fireEvent.click(saveButton()));
  await flush();
  expect(puts).toEqual([{ ifMatch: "3", body: { body: BODY.replace(`- [ ] ${AC2}`, `- [ ] ${rewritten}`), priority: 2 } }]);
  expect(save.seen[0]!.url).toBe(`${DEMO}/tickets/DEMO-2`);
  expect(toast()).toBe("Saved DEMO-2 as revision 4.");
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-2");
});

test("Discard puts every field back to the served text and sends nothing", async () => {
  await mount("#/tickets/demo/DEMO-2/edit");
  await type("Title", "Another title");
  await type("AC2", "Another criterion");
  await type("Evidence", "- nothing");
  expect(saveButton().disabled).toBe(false);
  await act(async () => void fireEvent.click(within(savePanel()).getByRole("button", { name: "Discard" })));
  for (const [name, value] of Object.entries(SERVED)) expect(box(name).value).toBe(value);
  expect(box("Evidence").value).toBe("- 01-notes.png: the run page with two notes.");
  expect(document.querySelector("[data-changed]")).toBeNull();
  expect(saveButton().disabled).toBe(true);
  expect(puts).toEqual([]);
});

test("a blocking check holds Save, a draft's problems do not, and a 409 keeps the edit", async () => {
  let blocks = true;
  const save = fakeFetch(() => Response.json({ error: "revision mismatch", current: 4 }, { status: 409 }));
  await mount("#/tickets/demo/DEMO-2/edit", { check: () => ({ problems: ["'Summary' is empty"], advisories: [], blocks }), save: save.fetchImpl });
  await type("Summary", "");
  await advance(600);
  expect(checks.at(-1)).toMatchObject({ ticket: "DEMO-2" });
  expect(saveButton().disabled).toBe(true);
  expect(within(savePanel()).getByText("Fix 1 error to save.")).toBeTruthy();
  blocks = false;
  await type("AC2", "Another criterion");
  await advance(600);
  expect(saveButton().disabled).toBe(false);
  expect(within(savePanel()).getByText("1 error · it saves as a draft and stays in Backlog.")).toBeTruthy();
  await act(async () => void fireEvent.click(saveButton()));
  await flush();
  expect(within(savePanel()).getByRole("alert").textContent).toBe("DEMO-2 changed since this page read it. Your edit is kept.");
  expect(box("Summary").value).toBe("");
  expect(box("AC2").value).toBe("Another criterion");
  expect(box("Title").value).toBe(SERVED.Title);
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-2/edit");
});
