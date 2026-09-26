import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { BoardWithLedger as Board } from "./ledger";
import { TicketSheet } from "../src/components/TicketSheet";
import type { BoardCard } from "../src/lib/board";
import type { Fetch } from "../src/lib/poll";
import type { TicketBody } from "../src/lib/ticket";
import { forgetToken, storeToken } from "../src/lib/token";
import type { Attention, BoardBody, ShippedBody, Status } from "../src/lib/types";
import { fixture, hostOf, settle } from "./harness";

const BASE = "http://writer:7710";
const threeDays = await fixture<{ now: number; shipped: ShippedBody }>("shipped_three_days.json");
const { now } = threeDays;
const MIN = 60_000;

const status: Status = {
  project: "/srv/dev/writer",
  host: "writer",
  now,
  supervisor: { state: "live", pid: 1, heartbeat_age_ms: 1000, host: "writer" },
  thresholds: { heartbeat_stale_ms: 180_000, strikes: 3 },
  runs: [{ id: 91, ticket: "KO-232", phase: "reviewing", heartbeat_age_ms: 4000, elapsed_ms: 15 * MIN, time_box_ms: 30 * MIN, host: "writer" }],
};
const attention: Attention = { level: "none", now, items: [] };

const wire = (ticket: string, title: string, run: number | null = null) => ({
  ticket,
  title,
  time_box_ms: 30 * MIN,
  run,
  question: null,
  waits_on: [],
  mirrored_ms: now,
});

const boardBody = (): BoardBody => ({
  now,
  columns: [
    { state: "needs_spec", tickets: [] },
    { state: "blocked_on_deps", tickets: [] },
    { state: "ready", tickets: [wire("KO-242", "Console reads /runs with cursor paging")] },
    { state: "blocked_on_operator", tickets: [] },
    { state: "in_flight", tickets: [wire("KO-232", "Split ledger writer from console reader", 91)] },
  ],
});

const ticket: TicketBody = {
  ticket: "KO-242",
  title: "Console reads /runs with cursor paging",
  status: "ready",
  body: "# Console reads /runs with cursor paging\n\n## Summary\n\nThe console pages **runs**.\n\n- [ ] one\n- [x] two\n\n```\nbun test\n```\n",
  acceptance_criteria: ["Given …"],
  verification_commands: ["bun test"],
  time_box_ms: 30 * MIN,
  run: null,
  mirrored_ms: now,
};

const host = hostOf(status, attention, BASE);

afterEach(() => {
  cleanup();
  forgetToken("writer:7710");
});

/** A daemon answering the Board's endpoints and `/tickets/KO-242` with
 *  `ticketAnswer`; `board` is read on each `/board` request so a test can
 *  swap it between polls. */
function daemon(ticketAnswer: () => Response, board: () => BoardBody = boardBody) {
  const asked: string[] = [];
  const fetch: Fetch = async (url) => {
    asked.push(url);
    if (url.endsWith("/board")) return Response.json(board());
    if (url.includes("/shipped")) return Response.json(threeDays.shipped);
    if (url.endsWith("/tickets/KO-242")) return ticketAnswer();
    return new Response("not found", { status: 404 });
  };
  return { fetch, asked };
}

const boardMarkup = () => screen.getByRole("region", { name: "Board" }).outerHTML;
const identifier = (id: string) => screen.getByRole("button", { name: id }) as HTMLButtonElement;

test("clicking the identifier opens an aria-modal dialog with the title and rendered body; the Board's DOM is unchanged but for the pressed state", async () => {
  const { fetch, asked } = daemon(() => Response.json(ticket));
  render(<Board hosts={[host]} now={now} deps={{ fetch }} tz="UTC" />);
  await act(settle);
  expect(screen.queryByRole("dialog")).toBeNull();
  const before = boardMarkup();
  expect(identifier("KO-242").getAttribute("aria-pressed")).toBe("false");

  fireEvent.click(identifier("KO-242"));
  await act(settle);
  const dialog = screen.getByRole("dialog");
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(asked).toContain(`${BASE}/tickets/KO-242`);
  expect(screen.getByRole("heading", { level: 2, name: "Console reads /runs with cursor paging" })).toBeTruthy();
  expect(dialog.querySelector("[data-sheet-state]")!.textContent).toBe("ready");
  const body = dialog.querySelector("[data-sheet-body]")!;
  expect(body.querySelector("h2")!.textContent).toBe("Summary");
  expect(body.querySelector("strong")!.textContent).toBe("runs");
  expect(Array.from(body.querySelectorAll("li input")).map((box) => (box as HTMLInputElement).checked)).toEqual([false, true]);
  expect(body.querySelector("pre")!.textContent).toBe("bun test\n");
  // The sheet is rendered beside the Board section, not in it.
  expect(screen.getByRole("region", { name: "Board" }).contains(dialog)).toBe(false);
  expect(identifier("KO-242").getAttribute("aria-pressed")).toBe("true");
  expect(boardMarkup().replace('aria-pressed="true"', 'aria-pressed="false"')).toBe(before);
});

test("Escape, the backdrop and the close button each dismiss the sheet and return focus to the identifier", async () => {
  const { fetch } = daemon(() => Response.json(ticket));
  render(<Board hosts={[host]} now={now} deps={{ fetch }} tz="UTC" />);
  await act(settle);

  const open = async () => {
    identifier("KO-242").focus();
    fireEvent.click(identifier("KO-242"));
    await act(settle);
    const dialog = screen.getByRole("dialog");
    expect(document.activeElement).toBe(dialog);
    return dialog;
  };
  const closed = () => {
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(identifier("KO-242"));
    expect(identifier("KO-242").getAttribute("aria-pressed")).toBe("false");
  };

  await open();
  fireEvent.keyDown(document, { key: "Escape" });
  closed();

  await open();
  fireEvent.click(document.querySelector("[data-backdrop]")!);
  closed();

  const dialog = await open();
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  closed();
  expect(document.body.contains(dialog)).toBe(false);
});

test("a poll that replaces the board data leaves the open sheet on the same ticket", async () => {
  let generation = 0;
  const board = () => {
    const body = boardBody();
    body.now = now + generation * MIN;
    body.columns[2]!.tickets[0]!.title = generation === 0 ? "Console reads /runs with cursor paging" : "Console reads /runs (renamed by poll)";
    return body;
  };
  const { fetch, asked } = daemon(() => Response.json(ticket), board);
  const { rerender } = render(<Board hosts={[host]} now={now} polls={0} deps={{ fetch }} tz="UTC" />);
  await act(settle);
  fireEvent.click(identifier("KO-242"));
  await act(settle);
  expect(screen.getByRole("dialog")).toBeTruthy();
  const fetches = asked.filter((url) => url.endsWith("/tickets/KO-242")).length;

  generation = 1;
  rerender(<Board hosts={[host]} now={now} polls={1} deps={{ fetch }} tz="UTC" />);
  await act(settle);
  expect(screen.getByText("Console reads /runs (renamed by poll)")).toBeTruthy();
  const dialog = screen.getByRole("dialog");
  expect(dialog.querySelector("h2")!.textContent).toBe("Console reads /runs with cursor paging");
  expect(dialog.querySelector("[data-sheet-body] pre")!.textContent).toBe("bun test\n");
  expect(identifier("KO-242").getAttribute("aria-pressed")).toBe("true");
  // The body is not refetched per poll: what the operator reads stays put.
  expect(asked.filter((url) => url.endsWith("/tickets/KO-242")).length).toBe(fetches);
});

test("a poll that moves the open ticket to another column re-mounts its card; Escape still returns focus to the identifier", async () => {
  let moved = false;
  const board = () => {
    const body = boardBody();
    if (moved) {
      body.now = now + MIN;
      body.columns[2]!.tickets = [];
      body.columns[4]!.tickets.push(wire("KO-242", "Console reads /runs with cursor paging", 92));
    }
    return body;
  };
  const { fetch } = daemon(() => Response.json(ticket), board);
  const { rerender } = render(<Board hosts={[host]} now={now} polls={0} deps={{ fetch }} tz="UTC" />);
  await act(settle);
  const before = identifier("KO-242");
  before.focus();
  fireEvent.click(before);
  await act(settle);
  expect(document.activeElement).toBe(screen.getByRole("dialog"));

  moved = true;
  rerender(<Board hosts={[host]} now={now} polls={1} deps={{ fetch }} tz="UTC" />);
  await act(settle);
  const after = identifier("KO-242");
  expect(after).not.toBe(before);
  expect(before.isConnected).toBe(false);
  expect(after.closest("[data-column]")!.getAttribute("data-column")).toBe("in_flight");
  expect(screen.getByRole("dialog")).toBeTruthy();

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(after);
  expect(after.getAttribute("aria-pressed")).toBe("false");
});

test("a 401 shows the host's needs-token line and a 404 says the ticket is not mirrored here; neither throws", async () => {
  let answer = 401;
  const { fetch } = daemon(() => new Response(answer === 401 ? "unauthorized" : "{}", { status: answer }));
  render(<Board hosts={[host]} now={now} deps={{ fetch }} tz="UTC" />);
  await act(settle);

  fireEvent.click(identifier("KO-242"));
  await act(settle);
  let dialog = screen.getByRole("dialog");
  expect(dialog.querySelector("[data-needs-token-line]")!.textContent).toBe("needs token");
  expect(dialog.querySelector("h2")!.textContent).toBe("Console reads /runs with cursor paging");
  fireEvent.keyDown(document, { key: "Escape" });

  answer = 404;
  fireEvent.click(identifier("KO-242"));
  await act(settle);
  dialog = screen.getByRole("dialog");
  expect(dialog.querySelector("[data-sheet-body]")!.textContent).toBe("not mirrored on this host");
  expect(dialog.querySelector("[role=alert]")).toBeNull();
});

// A native project on the host daemon, whose tickets the sheet may write.
const NATIVE = "http://writer:7710/projects/nat";

/** A host daemon's native board, each ticket at a revision: `GET
 *  /tickets/ID` serves it with `current`, and a write whose `If-Match`
 *  names the revision now lands as the next one while any other answers
 *  409 with `current`, as holophyte/serve_board.py does. Every request is
 *  recorded. */
function nativeDaemon(tickets: Record<string, { revision: number; body: string; column: string; run?: number }>) {
  const writes: { url: string; method: string; ifMatch: string | null; authorization: string | null; body: unknown }[] = [];
  const reads: string[] = [];
  const fetch: Fetch = async (url, init) => {
    const id = decodeURIComponent(url.slice(`${NATIVE}/tickets/`.length).split("/")[0]!);
    const held = tickets[id];
    if (!url.startsWith(`${NATIVE}/tickets/`) || held == null) return new Response("{}", { status: 404 });
    if (init?.method == null) {
      reads.push(url);
      const current = { revision: held.revision, at: now, author: "cli", title: id, body: held.body, priority: null, labels: [], column: held.column };
      return Response.json({ ...ticket, ticket: id, title: id, status: "ready", body: held.body, run: held.run ?? null, current, claimed: null });
    }
    const headers = new Headers(init.headers);
    const body = JSON.parse(init.body as string) as Record<string, string>;
    writes.push({ url, method: init.method, ifMatch: headers.get("if-match"), authorization: headers.get("authorization"), body });
    if (headers.get("if-match") !== String(held.revision)) {
      return Response.json({ error: "revision moved", current: held.revision }, { status: 409 });
    }
    held.revision += 1;
    if (typeof body.body === "string") held.body = body.body;
    if (typeof body.column === "string") held.column = body.column;
    return Response.json({ ticket: id, revision: held.revision, ...(url.endsWith("/cancel") ? { run: held.run ?? null } : {}) });
  };
  return { fetch, writes, reads };
}

/** A card for `id` on the native host, carrying live run `runId` if any. */
const cardFor = (id: string, runId: number | null = null): BoardCard => ({
  key: `${NATIVE}#${id}`,
  ticket: id,
  title: id,
  status: "ready",
  project: "nat",
  run: null,
  runId,
  strikesMax: 3,
  question: null,
  waitsOn: [],
  askedMs: null,
  now,
});

const sheet = (id: string, fetch: Fetch, runId: number | null = null) =>
  render(<TicketSheet host={{ base: NATIVE }} card={cardFor(id, runId)} onClose={() => {}} editable deps={{ fetch }} />);

test("Edit saves the changed body with one PUT at If-Match 3; a second sheet's save then answers 409, and its Reload shows revision 4", async () => {
  storeToken("writer:7710", "machine-token");
  const { fetch, writes, reads } = nativeDaemon({ "NAT-1": { revision: 3, body: "# First\n\nThe old body.\n", column: "ready" } });
  const first = within(sheet("NAT-1", fetch).container);
  const second = within(sheet("NAT-1", fetch).container);
  await act(settle);

  fireEvent.click(first.getByRole("button", { name: "Edit" }));
  const box = first.getByRole("textbox", { name: "Ticket body" }) as HTMLTextAreaElement;
  expect(box.value).toBe("# First\n\nThe old body.\n");
  fireEvent.change(box, { target: { value: "# First\n\nThe new body.\n" } });
  fireEvent.click(first.getByRole("button", { name: "Save" }));
  await act(settle);
  expect(writes).toEqual([
    { url: `${NATIVE}/tickets/NAT-1`, method: "PUT", ifMatch: "3", authorization: "Bearer machine-token", body: { body: "# First\n\nThe new body.\n" } },
  ]);
  expect(first.queryByRole("textbox", { name: "Ticket body" })).toBeNull();
  expect(first.getByText("The new body.")).toBeTruthy();

  fireEvent.click(second.getByRole("button", { name: "Edit" }));
  fireEvent.change(second.getByRole("textbox", { name: "Ticket body" }), { target: { value: "# First\n\nThe other body.\n" } });
  fireEvent.click(second.getByRole("button", { name: "Save" }));
  await act(settle);
  expect(writes.length).toBe(2);
  expect(writes[1]!.ifMatch).toBe("3");
  expect(second.getByRole("alert").textContent).toContain("This ticket changed since you opened it");
  expect(second.getByText("rev 3")).toBeTruthy();

  const readsBefore = reads.length;
  fireEvent.click(second.getByRole("button", { name: "Reload" }));
  await act(settle);
  expect(reads.length).toBe(readsBefore + 1);
  expect(second.queryByRole("alert")).toBeNull();
  expect(second.queryByRole("textbox", { name: "Ticket body" })).toBeNull();
  expect(second.getByText("rev 4")).toBeTruthy();
  expect(second.getByText("The new body.")).toBeTruthy();
});

test("Cancel on NAT-2 names run 7 and posts the note at If-Match 5; Move on NAT-1 in ready posts backlog at If-Match 3", async () => {
  const { fetch, writes } = nativeDaemon({
    "NAT-1": { revision: 3, body: "# One\n", column: "ready" },
    "NAT-2": { revision: 5, body: "# Two\n", column: "ready", run: 7 },
  });
  const cancel = within(sheet("NAT-2", fetch, 7).container);
  await act(settle);
  fireEvent.click(cancel.getByRole("button", { name: "Cancel" }));
  const confirm = cancel.getByRole("button", { name: "Cancel NAT-2 and end run 7" }) as HTMLButtonElement;
  fireEvent.change(cancel.getByRole("textbox", { name: "Cancel note" }), { target: { value: "wrong scope" } });
  fireEvent.click(confirm);
  await act(settle);
  expect(writes).toEqual([{ url: `${NATIVE}/tickets/NAT-2/cancel`, method: "POST", ifMatch: "5", authorization: null, body: { note: "wrong scope" } }]);
  cleanup();

  const move = within(sheet("NAT-1", fetch).container);
  await act(settle);
  expect(move.queryByRole("button", { name: "Move to Ready" })).toBeNull();
  fireEvent.click(move.getByRole("button", { name: "Move to Backlog" }));
  await act(settle);
  expect(writes.slice(1)).toEqual([{ url: `${NATIVE}/tickets/NAT-1/move`, method: "POST", ifMatch: "3", authorization: null, body: { column: "backlog" } }]);
  // The refetch shows the ticket in backlog, so Move now offers Ready.
  expect(move.getByRole("button", { name: "Move to Ready" })).toBeTruthy();
});

test("a 422 on Save lists every problem and keeps the edit open", async () => {
  const problems = ["Summary is still the template's placeholder", "Acceptance criteria has no checkbox"];
  const base = nativeDaemon({ "NAT-1": { revision: 3, body: "# One\n", column: "ready" } });
  const fetch: Fetch = async (url, init) => (init?.method === "PUT" ? Response.json({ problems }, { status: 422 }) : base.fetch(url, init));
  sheet("NAT-1", fetch);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Ticket body" }), { target: { value: "# One, edited\n" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await act(settle);
  const listed = document.querySelector("[data-problems]")!;
  expect(Array.from(listed.querySelectorAll("li")).map((item) => item.textContent)).toEqual(problems);
  expect((screen.getByRole("textbox", { name: "Ticket body" }) as HTMLTextAreaElement).value).toBe("# One, edited\n");
});

test("the Board's sheet has Edit, Move and Cancel only for a host whose /board answered editable", async () => {
  for (const editable of [true, false]) {
    const { fetch } = daemon(() => Response.json(ticket), () => ({ ...boardBody(), editable }));
    render(<Board hosts={[host]} now={now} deps={{ fetch }} tz="UTC" />);
    await act(settle);
    fireEvent.click(identifier("KO-242"));
    await act(settle);
    const dialog = within(screen.getByRole("dialog"));
    const buttons = ["Edit", "Move to Backlog", "Cancel"].map((name) => dialog.queryByRole("button", { name }));
    expect(buttons.every((found) => (editable ? found != null : found == null))).toBe(true);
    cleanup();
  }
});
