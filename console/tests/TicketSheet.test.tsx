import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { BoardWithLedger as Board } from "./ledger";
import type { Fetch } from "../src/lib/poll";
import type { TicketBody } from "../src/lib/ticket";
import type { Attention, BoardBody, ShippedBody, Status } from "../src/lib/types";
import { fixture, hostOf, settle } from "./harness";

const BASE = "http://writer:7710";
const threeDays = await fixture<{ now: number; shipped: ShippedBody }>("shipped_three_days.json");
const { now } = threeDays;
const MIN = 60_000;

const status: Status = {
  target: "/srv/dev/writer",
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

afterEach(cleanup);

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
  expect(body.querySelector("pre")!.textContent).toBe("bun test");
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
  expect(dialog.querySelector("[data-sheet-body] pre")!.textContent).toBe("bun test");
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
