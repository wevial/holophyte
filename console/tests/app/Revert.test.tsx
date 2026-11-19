import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, HistoryRow, RunDetail, Status, TicketDetail } from "../../src/lib/contract.gen";
import { writeView } from "../../src/app/components/ViewToggle";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, merged, MINUTE, NOW, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const servedBoard = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const servedHistory = (await Bun.file(new URL("history.json", SERVE)).json()) as { rows: HistoryRow[] };

const ENDED = NOW - 30 * MINUTE;
const runNine: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 9, ticket: "DEMO-9", title: "Page the ledger", phase: "done", outcome: "merged", merge_sha: "4f2a91c0ffee", ended_ms: ENDED, reverted: null },
};
const ticketNine: TicketDetail = {
  ...servedTicket,
  ticket: "DEMO-9",
  title: "Page the ledger",
  status: "merged",
  reverted: null,
  runs: [{ run: 9, attempt: 1, phase: "done", outcome: "merged", rounds: 1, pr_url: null, started_ms: ENDED - 20 * MINUTE, ended_ms: ENDED }],
};
const historyNine: HistoryRow = {
  ...servedHistory.rows[0]!, id: 9, ticket: "DEMO-9", title: "Page the ledger", phase: "done", outcome: "merged",
  started_ms: ENDED - 20 * MINUTE, ended_ms: ENDED, merge_sha: "4f2a91c0ffee", reverted: null,
};
const emptyBoard: Board = { ...servedBoard, columns: servedBoard.columns.map((column) => ({ ...column, tickets: [] })) };

interface Posted {
  url: string;
  body: Record<string, unknown>;
}

/** DEMO serves merged run 9 of DEMO-9 on every read, its status reporting
 *  `reverts`; each `POST /actions/revert` is kept and answered by `revert`. */
function daemon(reverts: boolean, revert: (body: Record<string, unknown>) => Response = () => new Response(null, { status: 500 })) {
  const posted: Posted[] = [];
  const polls = fleet({ items: [], shipped: [merged(9, "DEMO-9", ENDED)] }, { items: [] });
  const fetchImpl = async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      posted.push({ url, body });
      return revert(body);
    }
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions: true, reverts });
    if (url === `${DEMO}/runs/9`) return Response.json(runNine);
    if (url === `${DEMO}/tickets/DEMO-9`) return Response.json(ticketNine);
    if (url === `${DEMO}/board`) return Response.json(emptyBoard);
    if (url.startsWith(`${DEMO}/history?`)) {
      return Response.json({
        rows: [historyNine], since: 0, until: null, limit: 200, offset: 0, truncated: false, total: 1,
        counts: { state: { active: 0, merged: 1, failed: 0, abandoned: 0 }, failure: {}, implementer: {} }, now: NOW,
      });
    }
    return polls(url);
  };
  return { posted, fetchImpl };
}

async function mount(hash: string, fetchImpl: (url: string, init?: RequestInit) => Promise<Response>) {
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

async function openMenu(name: string): Promise<string[]> {
  await act(async () => void fireEvent.click(screen.getByRole("button", { name })));
  return within(screen.getByRole("menu", { name })).getAllByRole("menuitem").map((item) => item.textContent ?? "");
}

async function revertFromRunPage() {
  await openMenu("More for run 9");
  await act(async () => void fireEvent.click(screen.getByRole("menuitem", { name: "Revert…" })));
  for (let round = 0; round < 4; round += 1) await act(settle);
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

test("with reverts on, a merged run's Run page menu and its Runs row menu end with Revert…", async () => {
  const fake = daemon(true);
  await mount("#/runs/demo/9", fake.fetchImpl);
  expect((await openMenu("More for run 9")).at(-1)).toBe("Revert…");
  cleanup();

  await mount("#/runs?state=all", fake.fetchImpl);
  const row = document.querySelector<HTMLElement>('li[data-run="9"]')!;
  await act(async () => void fireEvent.click(within(row).getByRole("button", { name: "More for run 9" })));
  const items = within(screen.getByRole("menu", { name: "More for run 9" })).getAllByRole("menuitem").map((item) => item.textContent);
  expect(items).toEqual(["Open run", "Copy link", "Revert…"]);
});

test("with reverts off, neither the Run page menu nor the Runs row menu offers Revert", async () => {
  const fake = daemon(false);
  await mount("#/runs/demo/9", fake.fetchImpl);
  expect(await openMenu("More for run 9")).toEqual(["Open ticket", "Copy link"]);
  cleanup();

  await mount("#/runs?state=all", fake.fetchImpl);
  expect(await openMenu("More for run 9")).toEqual(["Open run", "Copy link"]);
});

test("a ticket whose latest run merged ends its Ticket page menu and its Tickets row menu with Revert…", async () => {
  const fake = daemon(true);
  await mount("#/tickets/demo/DEMO-9", fake.fetchImpl);
  expect((await openMenu("More for DEMO-9")).at(-1)).toBe("Revert…");
  cleanup();

  writeView("table");
  await mount("#/tickets", fake.fetchImpl);
  expect(await openMenu("More for DEMO-9")).toEqual(["Open ticket", "Copy link", "Revert…"]);
});

test("arrow keys move through the menu and Esc closes it, returning focus to its ⋯ button", async () => {
  await mount("#/runs/demo/9", daemon(true).fetchImpl);
  const button = screen.getByRole("button", { name: "More for run 9" });
  await act(async () => void fireEvent.keyDown(button, { key: "ArrowDown" }));
  const menu = screen.getByRole("menu", { name: "More for run 9" });
  expect(document.activeElement?.textContent).toBe("Open ticket");
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowDown" }));
  expect(document.activeElement?.textContent).toBe("Copy link");
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowUp" }));
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowUp" }));
  expect(document.activeElement?.textContent).toBe("Revert…");
  await act(async () => void fireEvent.keyDown(menu, { key: "Escape" }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(button);
});

test("Revert… previews, lists the later merge, and files the revert with the typed reason, naming the new ticket", async () => {
  const fake = daemon(true, (body) =>
    body.dry_run === true
      ? Response.json({ action: "revert", recorded: null, enabled: true, refused: null, dependents: [{ key: "DEMO-12", sha: "9be0d14aa1", paths: ["src/pages/ledger.tsx"] }] })
      : Response.json({ action: "revert", recorded: 42, key: "DEMO-50" }),
  );
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  expect(fake.posted).toEqual([{ url: `${DEMO}/actions/revert`, body: expect.objectContaining({ key: "DEMO-9", dry_run: true }) }]);

  const dialog = screen.getByRole("dialog", { name: "Revert DEMO-9?" });
  expect(within(dialog).getByText("demo · run 9")).toBeTruthy();
  const dependent = within(within(dialog).getByRole("list", { name: "Merged since, same files" })).getByRole("listitem");
  expect([...dependent.children].map((cell) => cell.textContent)).toEqual(["DEMO-12", "9be0d14", "src/pages/ledger.tsx"]);
  const file = within(dialog).getByRole("button", { name: "File revert" }) as HTMLButtonElement;
  expect(file.disabled).toBe(true);
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "  broke the ledger  " } }));
  expect(file.disabled).toBe(false);

  await act(async () => void fireEvent.click(file));
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(fake.posted[1]).toEqual({ url: `${DEMO}/actions/revert`, body: { key: "DEMO-9", reason: "broke the ledger" } });
  expect(screen.queryByRole("dialog")).toBeNull();
  const status = document.querySelector("[data-revert-filed]")!;
  expect(status.textContent).toBe("Filed DEMO-50 to revert DEMO-9.");
  expect(within(status as HTMLElement).getByRole("link", { name: "DEMO-50" }).getAttribute("href")).toEndWith("#/tickets/demo/DEMO-50");
});

test("a preview refused for store/schema.py names the path, offers only Close, and posts nothing more", async () => {
  const fake = daemon(true, () =>
    Response.json(
      { error: "DEMO-9 touched a refuse_paths path; nothing filed", paths: ["store/schema.py"], enabled: true, dependents: [], refused: { reason: "DEMO-9 touched a refuse_paths path", paths: ["store/schema.py"] } },
      { status: 409 },
    ),
  );
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();

  const dialog = screen.getByRole("dialog", { name: "DEMO-9 can't be reverted" });
  expect(within(dialog).getByText("store/schema.py").tagName).toBe("CODE");
  expect(dialog.querySelector("[data-revert-refusal]")?.textContent).toBe("It changed store/schema.py, which this project never reverts. Ship a forward fix instead.");
  expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual(["Close"]);
  expect(within(dialog).queryByRole("textbox")).toBeNull();

  await act(async () => void fireEvent.click(within(dialog).getByRole("button", { name: "Close" })));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(fake.posted).toHaveLength(1);
});
