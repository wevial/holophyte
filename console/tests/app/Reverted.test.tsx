import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, HistoryRow, Reverted, RunDetail, TicketDetail } from "../../src/lib/contract.gen";
import { writeView } from "../../src/app/components/ViewToggle";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, merged, MINUTE, NOW, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const servedBoard = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const servedHistory = (await Bun.file(new URL("history.json", SERVE)).json()) as { rows: HistoryRow[] };

const REVERTED: Reverted = { by: "HOLO-9", sha: "77aa01b9e2", reason: "broke the header" };
const ENDED = NOW - 30 * MINUTE;

const historyRow = (id: number, ticket: string, reverted: Reverted | null): HistoryRow => ({
  ...servedHistory.rows[0]!, id, ticket, title: `Change ${ticket}`, phase: "done", outcome: "merged",
  started_ms: ENDED - 20 * MINUTE, ended_ms: ENDED, merge_sha: "4f2a91c0ffee", pr_url: null, reverted,
});
const runSeven: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 7, ticket: "DEMO-7", title: "Page the ledger", phase: "done", outcome: "merged", merge_sha: "4f2a91c0ffee", ended_ms: ENDED, reverted: REVERTED },
};
const ticketSeven: TicketDetail = { ...servedTicket, ticket: "DEMO-7", title: "Page the ledger", status: "backlog", reverted: REVERTED };
const listed = servedBoard.columns.flatMap((column) => column.tickets)[0]!;
const board = (state: Board["columns"][number]["state"]): Board => ({
  ...servedBoard,
  stories: [],
  columns: [{ state, tickets: [{ ...listed, ticket: "DEMO-7", title: "Page the ledger", run: 7, reverted: REVERTED }] }],
});

/** DEMO answers its history with merged runs 7 (reverted by `REVERTED`)
 *  and 8 (not reverted), run 7's page, DEMO-7's ticket, and a board listing
 *  DEMO-7, reverted, in `column`; run 7's merge is shipped today. */
function daemon(column: Board["columns"][number]["state"] = "backlog") {
  const polls = fleet({ items: [], shipped: [merged(7, "DEMO-7", ENDED)] }, { items: [] });
  return async (url: string) => {
    if (url === `${DEMO}/runs/7`) return Response.json(runSeven);
    if (url === `${DEMO}/tickets/DEMO-7`) return Response.json(ticketSeven);
    if (url === `${DEMO}/board`) return Response.json(board(column));
    if (url.startsWith(`${DEMO}/history?`)) {
      return Response.json({
        rows: [historyRow(8, "DEMO-8", null), historyRow(7, "DEMO-7", REVERTED)], since: 0, until: null, limit: 200, offset: 0, truncated: false, total: 2,
        counts: { state: { active: 0, merged: 2, failed: 0, abandoned: 0 }, failure: {}, implementer: {} }, now: NOW,
      });
    }
    return polls(url);
  };
}

async function mount(hash: string, fetchImpl: (url: string) => Promise<Response>) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});
afterAll(() => happyDOM.setURL("about:blank"));

test("on Runs a reverted run's row shows the undo glyph and the word reverted, and a merged run beside it keeps its merged glyph", async () => {
  await mount("#/runs?state=all", daemon());
  const reverted = document.querySelector<HTMLElement>('li[data-run="7"]')!;
  expect(within(reverted).getByRole("img", { name: "merged 11:30 · reverted by HOLO-9" }).getAttribute("data-state")).toBe("reverted");
  expect(reverted.querySelector("[data-pr]")?.textContent).toBe("reverted");
  const narrow = reverted.querySelector("[data-title] [data-reverted]")!;
  expect(narrow.textContent).toBe("reverted");
  expect(narrow.classList.contains("min-[760px]:@min-[600px]:hidden")).toBe(true);
  expect(reverted.querySelector("[data-title] > span")!.classList.contains("text-ink-2")).toBe(true);
  const kept = document.querySelector<HTMLElement>('li[data-run="8"]')!;
  expect(kept.querySelector('[data-state="merged"]')).not.toBeNull();
  expect(kept.querySelector('[data-state="reverted"]')).toBeNull();
  expect(kept.textContent).not.toContain("reverted");
});

test("on Tickets a reverted ticket's row reads reverted · Backlog beside the undo glyph", async () => {
  writeView("table");
  await mount("#/tickets", daemon());
  const status = document.querySelector<HTMLElement>('[data-row][data-column="backlog"] [data-cell="status"]')!;
  expect(status.textContent).toBe("reverted · Backlog");
  expect(within(status).getByRole("img", { name: "reverted" }).getAttribute("data-state")).toBe("reverted");
  expect([...document.querySelectorAll("[data-row]")].map((row) => row.getAttribute("data-column"))).toEqual(["backlog"]);
});

test("a reverted run's page says it merged then was reverted by the revert ticket, linked, with the reason", async () => {
  await mount("#/runs/demo/7", daemon());
  const verdict = document.querySelector<HTMLElement>("[data-run-verdict]")!;
  expect(within(verdict).getByRole("status").textContent).toBe("Merged into main as 4f2a91c, then reverted by HOLO-9: “broke the header”");
  expect(within(verdict).getByRole("link", { name: "HOLO-9" }).getAttribute("href")).toBe("/#/tickets/demo/HOLO-9");
  expect(within(verdict).getByRole("img", { name: "reverted" })).toBeTruthy();
});

test("a reverted ticket's page names the revert ticket and the reason in place of its Backlog column", async () => {
  await mount("#/tickets/demo/DEMO-7", daemon());
  const line = screen.getByRole("status", { name: "Revert" });
  expect(line.textContent).toBe("reverted by HOLO-9: “broke the header”");
  expect(within(line).getByRole("link", { name: "HOLO-9" }).getAttribute("href")).toBe("/#/tickets/demo/HOLO-9");
  expect(screen.queryByRole("status", { name: "Board" })).toBeNull();
});

test("a reverted ticket claimed again keeps its board column beside the revert on its page", async () => {
  await mount("#/tickets/demo/DEMO-7", daemon("in_flight"));
  expect(screen.getByRole("status", { name: "Revert" }).textContent).toBe("reverted by HOLO-9: “broke the header”");
  expect(screen.getByRole("status", { name: "Board" }).textContent).toMatch(/^In flight/);
});
