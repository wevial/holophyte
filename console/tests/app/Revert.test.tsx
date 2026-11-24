import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, HistoryRow, RunDetail, Status, TicketDetail } from "../../src/lib/contract.gen";
import { writeView } from "../../src/app/components/ViewToggle";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { blocked, DEMO, fleet, merged, MINUTE, NOW, SERVE } from "./needsFleet";

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

interface Served {
  runs: Record<number, RunDetail>;
  tickets: Record<string, TicketDetail>;
  board: Board;
}

/** DEMO serves `served` (by default merged run 9 of DEMO-9, shipped today,
 *  and an empty board), read afresh on every request, its status reporting
 *  `reverts`, with `needs` its Needs you entries; each `POST /actions/revert`
 *  is kept and answered by `revert`, and each ticket read is kept by key. */
function daemon(
  reverts: boolean,
  revert: (body: Record<string, unknown>) => Response | Promise<Response> = () => new Response(null, { status: 500 }),
  served: Partial<Served> = {},
  shipped = [merged(9, "DEMO-9", ENDED)],
  needs: (typeof blocked)[] = [],
) {
  const posted: Posted[] = [];
  const ticketReads: string[] = [];
  const state: Served = { runs: { 9: runNine }, tickets: { "DEMO-9": ticketNine }, board: emptyBoard, ...served };
  const polls = fleet({ items: needs, shipped }, { items: [] });
  const fetchImpl = async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      posted.push({ url, body });
      return revert(body);
    }
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions: true, reverts });
    const run = /^\/runs\/(\d+)$/.exec(url.slice(DEMO.length))?.[1];
    if (url.startsWith(DEMO) && run != null && state.runs[Number(run)]) return Response.json(state.runs[Number(run)]);
    const ticket = /^\/tickets\/([A-Z]+-\d+)$/.exec(url.slice(DEMO.length))?.[1];
    if (url.startsWith(DEMO) && ticket != null) ticketReads.push(ticket);
    if (url.startsWith(DEMO) && ticket != null && state.tickets[ticket]) return Response.json(state.tickets[ticket]);
    if (url === `${DEMO}/board`) return Response.json(state.board);
    if (url.startsWith(`${DEMO}/history?`)) {
      return Response.json({
        rows: [historyNine], since: 0, until: null, limit: 200, offset: 0, truncated: false, total: 1,
        counts: { state: { active: 0, merged: 1, failed: 0, abandoned: 0 }, failure: {}, implementer: {} }, now: NOW,
      });
    }
    return polls(url);
  };
  return { posted, ticketReads, state, fetchImpl };
}

const PREVIEW = { action: "revert", recorded: null, enabled: true, refused: null, dependents: [] };

async function mount(hash: string, fetchImpl: (url: string, init?: RequestInit) => Promise<Response>, client = consoleQueryClient()) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={client}>
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

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void; setViewport: (viewport: { width: number }) => void } }).happyDOM;

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

test("a Done row with its menu open names its project beside the title and ends on its merge time, with no estimate", async () => {
  writeView("table");
  await mount("#/tickets", daemon(true).fetchImpl);
  const row = document.querySelector<HTMLElement>('[data-row][data-column="done"]')!;
  expect(row.querySelector('[data-cell="estimate"]')).not.toBeNull();
  await openMenu("More for DEMO-9");
  expect(row.querySelector('[data-cell="title"] [data-cell="project"]')?.textContent).toBe("demo");
  expect([...row.children].map((cell) => cell.getAttribute("data-cell"))).toEqual(["status", "key", "title", "stage"]);
  expect(row.querySelector('[data-cell="stage"]')?.textContent).toMatch(/^merged /);
});

test("a reverted run's Revert… is disabled, names its revert ticket, and posts nothing when pressed", async () => {
  const reverted = { ...runNine, run: { ...runNine.run, reverted: { by: "DEMO-50", sha: "77aa01b", reason: "broke the ledger" } } };
  const fake = daemon(true, undefined, { runs: { 9: reverted } });
  await mount("#/runs/demo/9", fake.fetchImpl);
  await openMenu("More for run 9");
  const item = screen.getByRole("menuitem", { name: "Revert…" });
  expect(item.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(item.getAttribute("aria-describedby")!)?.textContent).toBe("Reverted by DEMO-50");
  await act(async () => void fireEvent.click(item));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(fake.posted).toEqual([]);
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(screen.getByRole("menu", { name: "More for run 9" })).toBeTruthy();
});

test("a run that is not its ticket's latest merge is refused before any preview, naming the run a revert would undo", async () => {
  const remerged = { ...ticketNine, runs: [{ ...ticketNine.runs[0]!, run: 11 }, ...ticketNine.runs] };
  const fake = daemon(true, undefined, { tickets: { "DEMO-9": remerged } });
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  const dialog = screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" });
  expect(dialog.querySelector("[data-revert-refusal]")?.textContent).toBe("Run 9 is not DEMO-9's latest merge; a revert would undo run 11. Revert it from run 11.");
  expect(fake.posted).toEqual([]);
});

test("a latest merge with no recorded sha is refused before any preview, naming the older merge the daemon would undo or that there is none", async () => {
  const shaless = { ...runNine, run: { ...runNine.run, merge_sha: null } };
  const refusal = () => screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" }).querySelector("[data-revert-refusal]")?.textContent;
  const only = daemon(true, undefined, { runs: { 9: shaless } });
  await mount("#/runs/demo/9", only.fetchImpl);
  await revertFromRunPage();
  expect(refusal()).toBe("Run 9 recorded no merge sha, so there is no merge to revert. Ship a forward fix instead.");
  cleanup();

  const again = daemon(true, undefined, { runs: { 9: shaless }, tickets: { "DEMO-9": { ...ticketNine, runs: [...ticketNine.runs, { ...ticketNine.runs[0]!, run: 5 }] } } });
  await mount("#/runs/demo/9", again.fetchImpl);
  await revertFromRunPage();
  expect(refusal()).toBe("Run 9 recorded no merge sha, so a revert would undo an older merge of DEMO-9. Ship a forward fix instead.");
  expect([...only.posted, ...again.posted]).toEqual([]);
});

test("a Done row reads its ticket only once its menu opens: a reverted one disables Revert… naming the revert ticket, a newer unmerged attempt drops it", async () => {
  const doneMenu = async (ticket: TicketDetail) => {
    writeView("table");
    const fake = daemon(true, undefined, { tickets: { "DEMO-9": ticket } });
    await mount("#/tickets", fake.fetchImpl);
    expect(fake.ticketReads).toEqual([]);
    await act(async () => void fireEvent.click(screen.getByRole("button", { name: "More for DEMO-9" })));
    for (let round = 0; round < 4; round += 1) await act(settle);
    expect(fake.ticketReads).toContain("DEMO-9");
    return screen.queryByRole("menuitem", { name: "Revert…" });
  };
  const item = await doneMenu({ ...ticketNine, reverted: { by: "DEMO-50", sha: "77aa01b", reason: "broke the ledger" } });
  expect(item?.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(item!.getAttribute("aria-describedby")!)?.textContent).toBe("Reverted by DEMO-50");
  cleanup();

  const retried = { ...ticketNine, runs: [{ ...ticketNine.runs[0]!, run: 12, outcome: "failed" }, ...ticketNine.runs] };
  expect(await doneMenu(retried)).toBeNull();
});

test("a Backlog row and a Needs you row whose latest run merged and was reverted end their menu with Revert… disabled, naming the revert ticket", async () => {
  const reverted = { by: "DEMO-50", sha: "77aa01b", reason: "broke the ledger" };
  const card = { ...servedBoard.columns.find((column) => column.tickets.length > 0)!.tickets[0]!, ticket: "DEMO-9", title: "Page the ledger", run: null, reverted };
  const board: Board = { ...emptyBoard, columns: [...emptyBoard.columns, { ...emptyBoard.columns[0]!, state: "backlog", tickets: [card] }] };
  for (const [column, needs] of [["backlog", []], ["needs_you", [{ ...blocked, ticket: "DEMO-9", run: 9 }]]] as const) {
    writeView("table");
    const fake = daemon(true, undefined, { board, tickets: { "DEMO-9": { ...ticketNine, status: "backlog", reverted } } }, [], [...needs]);
    await mount("#/tickets", fake.fetchImpl);
    expect(document.querySelector("[data-row]")?.getAttribute("data-column")).toBe(column);
    await act(async () => void fireEvent.click(screen.getByRole("button", { name: "More for DEMO-9" })));
    for (let round = 0; round < 4; round += 1) await act(settle);
    const items = within(screen.getByRole("menu", { name: "More for DEMO-9" })).getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual(["Open ticket", "Copy link", "Revert…"]);
    expect(items.at(-1)!.getAttribute("aria-disabled")).toBe("true");
    expect(document.getElementById(items.at(-1)!.getAttribute("aria-describedby")!)?.textContent).toBe("Reverted by DEMO-50");
    cleanup();
  }
});

test("Open ticket from a project-scoped Run page keeps the project scope", async () => {
  await mount("#/runs/demo/9?project=demo", daemon(true).fetchImpl);
  await openMenu("More for run 9");
  await act(async () => void fireEvent.click(screen.getByRole("menuitem", { name: "Open ticket" })));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-9?project=demo");
});

test("Esc still returns focus to ⋯ after the focused Revert… leaves a Done row's menu once its ticket shows a newer unmerged attempt", async () => {
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const retried = { ...ticketNine, runs: [{ ...ticketNine.runs[0]!, run: 12, outcome: "failed" }, ...ticketNine.runs] };
  const fake = daemon(true, undefined, { tickets: { "DEMO-9": retried } });
  writeView("table");
  await mount("#/tickets", async (url, init) => {
    if (url === `${DEMO}/tickets/DEMO-9`) await held;
    return fake.fetchImpl(url, init);
  });
  const button = screen.getByRole("button", { name: "More for DEMO-9" });
  await act(async () => void fireEvent.keyDown(button, { key: "ArrowUp" }));
  expect(document.activeElement?.textContent).toBe("Revert…");
  await act(async () => release());
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(screen.queryByRole("menuitem", { name: "Revert…" })).toBeNull();
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(button);
});

test("a ticket that merged again after its preview files nothing and says why", async () => {
  const fake = daemon(true, () => Response.json(PREVIEW));
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  fake.state.tickets["DEMO-9"] = { ...ticketNine, runs: [{ ...ticketNine.runs[0]!, run: 11 }, ...ticketNine.runs] };
  const dialog = screen.getByRole("alertdialog", { name: "Revert DEMO-9?" });
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "broke the ledger" } }));
  await act(async () => void fireEvent.click(within(dialog).getByRole("button", { name: "File revert" })));
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" }).querySelector("[data-revert-refusal]")?.textContent).toContain("a revert would undo run 11");
  expect(fake.posted.map(({ body }) => body.dry_run)).toEqual([true]);
});

async function fileFromDialog(reason = "broke the ledger") {
  const dialog = screen.getByRole("alertdialog", { name: "Revert DEMO-9?" });
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: reason } }));
  await act(async () => void fireEvent.click(within(dialog).getByRole("button", { name: "File revert" })));
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const refusalText = () => screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" }).querySelector("[data-revert-refusal]")?.textContent;

test("a revert previewed from the Ticket page files nothing once its newest attempt shows a failed run", async () => {
  const fake = daemon(true, () => Response.json(PREVIEW));
  await mount("#/tickets/demo/DEMO-9", fake.fetchImpl);
  await openMenu("More for DEMO-9");
  await act(async () => void fireEvent.click(screen.getByRole("menuitem", { name: "Revert…" })));
  for (let round = 0; round < 4; round += 1) await act(settle);
  fake.state.tickets["DEMO-9"] = { ...ticketNine, runs: [{ ...ticketNine.runs[0]!, run: 11, outcome: "failed" }, ...ticketNine.runs] };
  await fileFromDialog();
  expect(refusalText()).toContain("newest attempt, run 11, did not merge");
  expect(fake.posted.map(({ body }) => body.dry_run)).toEqual([true]);
});

test("a run whose revert completed after its preview files nothing, naming the revert ticket", async () => {
  const fake = daemon(true, () => Response.json(PREVIEW));
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  fake.state.runs[9] = { ...runNine, run: { ...runNine.run, reverted: { by: "DEMO-50", sha: "77aa01b", reason: "broke the ledger" } } };
  await fileFromDialog();
  expect(refusalText()).toBe("Run 9 of DEMO-9 was already reverted by DEMO-50.");
  expect(fake.posted.map(({ body }) => body.dry_run)).toEqual([true]);
});

test("a preview answered 200 without its preview fields offers no filing and posts nothing more", async () => {
  for (const reply of [{}, { ...PREVIEW, dependents: [{ key: "DEMO-12" }] }]) {
    const fake = daemon(true, () => Response.json(reply));
    await mount("#/runs/demo/9", fake.fetchImpl);
    await revertFromRunPage();
    const dialog = screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" });
    expect(within(dialog).queryByText("Nothing merged since touches these files")).toBeNull();
    expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual(["Close"]);
    expect(fake.posted).toHaveLength(1);
    cleanup();
  }
});

test("a Done row whose ticket read fails keeps Revert… disabled and saying so", async () => {
  writeView("table");
  const fake = daemon(true);
  await mount("#/tickets", async (url, init) => (url === `${DEMO}/tickets/DEMO-9` ? new Response(null, { status: 503 }) : fake.fetchImpl(url, init)));
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "More for DEMO-9" })));
  for (let round = 0; round < 6; round += 1) await act(settle);
  const item = screen.getByRole("menuitem", { name: "Revert…" });
  expect(item.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(item.getAttribute("aria-describedby")!)?.textContent).toBe("Couldn't read DEMO-9");
});

test("a row's Revert… read once disables again while its ticket is read afresh and after that read fails", async () => {
  writeView("table");
  const fake = daemon(true);
  const client = consoleQueryClient();
  let held: { promise: Promise<Response>; reject: (failure: Error) => void } | null = null;
  await mount("#/tickets", async (url, init) => (url === `${DEMO}/tickets/DEMO-9` && held != null ? held.promise : fake.fetchImpl(url, init)), client);
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "More for DEMO-9" })));
  for (let round = 0; round < 6; round += 1) await act(settle);
  const revert = () => screen.getByRole("menuitem", { name: "Revert…" });
  const note = () => document.getElementById(revert().getAttribute("aria-describedby") ?? "")?.textContent;
  expect(revert().getAttribute("aria-disabled")).not.toBe("true");
  held = Promise.withResolvers<Response>();
  await act(async () => void client.invalidateQueries({ queryKey: ["route", DEMO, "/tickets/DEMO-9"] }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(revert().getAttribute("aria-disabled")).toBe("true");
  expect(note()).toBe("Checking…");
  held.reject(new Error("daemon gone"));
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(revert().getAttribute("aria-disabled")).toBe("true");
  expect(note()).toBe("Couldn't read DEMO-9");
});

test("a File revert refused on its recheck moves focus from the vanished button to Close", async () => {
  const fake = daemon(true, () => Response.json(PREVIEW));
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  fake.state.tickets["DEMO-9"] = { ...ticketNine, runs: [{ ...ticketNine.runs[0]!, run: 11 }, ...ticketNine.runs] };
  const dialog = screen.getByRole("alertdialog", { name: "Revert DEMO-9?" });
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "broke the ledger" } }));
  const file = within(dialog).getByRole("button", { name: "File revert" });
  file.focus();
  await act(async () => void fireEvent.click(file));
  for (let round = 0; round < 4; round += 1) await act(settle);
  const dialogNow = screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" });
  expect(dialogNow.contains(document.activeElement)).toBe(true);
  expect(document.activeElement?.textContent).toBe("Close");
});

test("a filing in flight holds the dialog open against Keep it and Esc", async () => {
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const fake = daemon(true, async (body) => {
    if (body.dry_run === true) return Response.json(PREVIEW);
    await held;
    return Response.json({ action: "revert", recorded: 42, key: "DEMO-50" });
  });
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  const dialog = screen.getByRole("alertdialog", { name: "Revert DEMO-9?" });
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "broke the ledger" } }));
  await act(async () => void fireEvent.click(within(dialog).getByRole("button", { name: "File revert" })));
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect((within(dialog).getByRole("button", { name: "Keep it" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => void fireEvent.click(within(dialog).getByRole("button", { name: "Keep it" })));
  await act(async () => void fireEvent.keyDown(document, { key: "Escape" }));
  expect(screen.getByRole("alertdialog", { name: "Revert DEMO-9?" })).toBe(dialog);
  await act(async () => release());
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(document.querySelector("[data-revert-filed]")?.textContent).toBe("Filed DEMO-50 to revert DEMO-9.");
});

test("a slow preview from one Done row does not open over the dialog of a row chosen after it", async () => {
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const runEight = { ...runNine, run: { ...runNine.run, id: 8, ticket: "DEMO-8" } };
  const ticketEight = { ...ticketNine, ticket: "DEMO-8", runs: [{ ...ticketNine.runs[0]!, run: 8 }] };
  const fake = daemon(
    true,
    async (body) => {
      if (body.key === "DEMO-8") await held;
      return Response.json(PREVIEW);
    },
    { runs: { 8: runEight, 9: runNine }, tickets: { "DEMO-8": ticketEight, "DEMO-9": ticketNine } },
    [merged(9, "DEMO-9", ENDED), merged(8, "DEMO-8", ENDED - MINUTE)],
  );
  writeView("table");
  await mount("#/tickets", fake.fetchImpl);
  for (const key of ["DEMO-8", "DEMO-9"]) {
    await openMenu(`More for ${key}`);
    for (let round = 0; round < 4; round += 1) await act(settle);
    await act(async () => void fireEvent.click(screen.getByRole("menuitem", { name: "Revert…" })));
    for (let round = 0; round < 4; round += 1) await act(settle);
  }
  await act(async () => release());
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(screen.getAllByRole("alertdialog").map((dialog) => dialog.querySelector("h2")?.textContent)).toEqual(["Revert DEMO-9?"]);
  expect(screen.queryAllByRole("dialog")).toHaveLength(0);
});

test("a preview answered after Raw log opened leaves Raw log the only dialog, and one Esc closes it", async () => {
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const fake = daemon(true, async () => {
    await held;
    return Response.json(PREVIEW);
  });
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: /^Raw log/ })));
  await act(async () => release());
  for (let round = 0; round < 4; round += 1) await act(settle);
  const dialogs = [...screen.queryAllByRole("dialog"), ...screen.queryAllByRole("alertdialog")];
  expect(dialogs).toHaveLength(1);
  expect(dialogs[0]!.textContent).toContain("Raw log · run 9");
  await act(async () => void fireEvent.keyDown(document.activeElement ?? document, { key: "Escape" }));
  expect([...screen.queryAllByRole("dialog"), ...screen.queryAllByRole("alertdialog")]).toHaveLength(0);
});

test("a preview answered after Raw log opened and closed again opens no dialog", async () => {
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const fake = daemon(true, async () => {
    await held;
    return Response.json(PREVIEW);
  });
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: /^Raw log/ })));
  await act(async () => void fireEvent.keyDown(document.activeElement ?? document, { key: "Escape" }));
  expect([...screen.queryAllByRole("dialog"), ...screen.queryAllByRole("alertdialog")]).toHaveLength(0);
  await act(async () => release());
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect([...screen.queryAllByRole("dialog"), ...screen.queryAllByRole("alertdialog")]).toHaveLength(0);
});

test("a preview answered after the narrow-width sidebar drawer opened and closed again opens no dialog", async () => {
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const fake = daemon(true, async () => {
    await held;
    return Response.json(PREVIEW);
  });
  happyDOM.setViewport({ width: 390 });
  try {
    await mount("#/runs/demo/9", fake.fetchImpl);
    await revertFromRunPage();
    await act(async () => void fireEvent.keyDown(document, { key: "\\", ctrlKey: true }));
    expect(screen.getByRole("dialog", { name: "Sidebar" })).toBeTruthy();
    await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
    expect([...screen.queryAllByRole("dialog"), ...screen.queryAllByRole("alertdialog")]).toHaveLength(0);
    await act(async () => release());
    for (let round = 0; round < 4; round += 1) await act(settle);
    expect([...screen.queryAllByRole("dialog"), ...screen.queryAllByRole("alertdialog")]).toHaveLength(0);
  } finally {
    happyDOM.setViewport({ width: 1024 });
  }
});

test("Copy link on a plain-HTTP origin copies the run's link through a selection and says so", async () => {
  const secure = Object.getOwnPropertyDescriptor(window, "isSecureContext");
  const exec = document.execCommand;
  let copied = "";
  Object.defineProperty(window, "isSecureContext", { value: false, configurable: true });
  document.execCommand = ((command: string) => {
    if (command !== "copy") return false;
    const area = document.activeElement as HTMLTextAreaElement;
    copied = area.value.slice(area.selectionStart, area.selectionEnd);
    return true;
  }) as typeof document.execCommand;
  try {
    await mount("#/runs/demo/9", daemon(true).fetchImpl);
    await openMenu("More for run 9");
    await act(async () => void fireEvent.click(screen.getByRole("menuitem", { name: "Copy link" })));
    for (let round = 0; round < 2; round += 1) await act(settle);
    expect(copied).toBe(`${DEMO}/#/runs/demo/9`);
    expect(document.querySelector("[data-toast]")?.textContent).toBe("Copied link.");
  } finally {
    document.execCommand = exec;
    if (secure) Object.defineProperty(window, "isSecureContext", secure);
    else delete (window as { isSecureContext?: boolean }).isSecureContext;
  }
});

test("a merged run's menu with reverts on places a separator right before Revert…", async () => {
  await mount("#/runs/demo/9", daemon(true).fetchImpl);
  await openMenu("More for run 9");
  const menu = screen.getByRole("menu", { name: "More for run 9" });
  const separator = within(menu).getByRole("separator");
  const items = within(menu).getAllByRole("menuitem");
  expect(items.map((item) => item.textContent)).toEqual(["Open ticket", "Copy link", "Revert…"]);
  expect(items).not.toContain(separator);
  expect(separator.nextElementSibling).toBe(items.at(-1)!);
});

test("typing a letter in an open menu focuses the matching item", async () => {
  await mount("#/runs/demo/9", daemon(true).fetchImpl);
  await act(async () => void fireEvent.keyDown(screen.getByRole("button", { name: "More for run 9" }), { key: "ArrowDown" }));
  expect(document.activeElement?.textContent).toBe("Open ticket");
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "c" }));
  expect(document.activeElement?.textContent).toBe("Copy link");
  await act(() => new Promise<void>((resolve) => setTimeout(resolve, 550)));
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "r" }));
  expect(document.activeElement?.textContent).toBe("Revert…");
});

test("arrow keys wrap at both ends, Home and End jump to the ends, and Esc closes the menu returning focus to ⋯", async () => {
  await mount("#/runs/demo/9", daemon(true).fetchImpl);
  const button = screen.getByRole("button", { name: "More for run 9" });
  await act(async () => void fireEvent.keyDown(button, { key: "ArrowDown" }));
  const menu = screen.getByRole("menu", { name: "More for run 9" });
  expect(document.activeElement?.textContent).toBe("Open ticket");
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowDown" }));
  expect(document.activeElement?.textContent).toBe("Copy link");
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowDown" }));
  expect(document.activeElement?.textContent).toBe("Revert…");
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowDown" }));
  expect(document.activeElement?.textContent).toBe("Open ticket");
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowUp" }));
  expect(document.activeElement?.textContent).toBe("Revert…");
  await act(async () => void fireEvent.keyDown(menu, { key: "ArrowUp" }));
  expect(document.activeElement?.textContent).toBe("Copy link");
  await act(async () => void fireEvent.keyDown(menu, { key: "Home" }));
  expect(document.activeElement?.textContent).toBe("Open ticket");
  await act(async () => void fireEvent.keyDown(menu, { key: "End" }));
  expect(document.activeElement?.textContent).toBe("Revert…");
  await act(async () => void fireEvent.keyDown(menu, { key: "Escape" }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(button);
});

test("Enter or ArrowDown on ⋯ focuses the first item, and ArrowUp focuses the last", async () => {
  const user = userEvent.setup();
  await mount("#/runs/demo/9", daemon(true).fetchImpl);
  const button = screen.getByRole("button", { name: "More for run 9" });

  for (const [chord, expected] of [
    ["{Enter}", "Open ticket"],
    ["{ArrowDown}", "Open ticket"],
    ["{ArrowUp}", "Revert…"],
  ] as const) {
    button.focus();
    await user.keyboard(chord);
    await act(settle);
    expect(screen.getByRole("menu", { name: "More for run 9" })).toBeTruthy();
    expect(document.activeElement?.textContent).toBe(expected);
    await user.keyboard("{Escape}");
    await act(settle);
    expect(screen.queryByRole("menu")).toBeNull();
  }
});

test.each(["Enter", " "])("holding the %p that opened the menu chooses no item, and a fresh Enter chooses the focused one", async (key) => {
  const user = userEvent.setup();
  await mount("#/runs/demo/9?project=demo", daemon(true).fetchImpl);
  screen.getByRole("button", { name: "More for run 9" }).focus();
  await user.keyboard(key === " " ? " " : "{Enter}");
  await act(settle);
  const item = screen.getByRole("menuitem", { name: "Open ticket" });
  expect(document.activeElement).toBe(item);
  let browserMayActivate = true;
  await act(async () => void (browserMayActivate = fireEvent.keyDown(item, { key, repeat: true })));
  expect(browserMayActivate).toBe(false);
  await act(async () => void fireEvent.keyUp(item, { key }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(screen.queryByRole("menu")).not.toBeNull();
  expect(window.location.hash).toBe("#/runs/demo/9?project=demo");
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Enter" }));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-9?project=demo");
});

test("a scroll inside an open menu keeps it open, and a page scroll closes it", async () => {
  await mount("#/runs/demo/9", daemon(true).fetchImpl);
  await openMenu("More for run 9");
  await act(async () => void fireEvent.scroll(screen.getByRole("menu", { name: "More for run 9" })));
  expect(screen.queryByRole("menu")).not.toBeNull();
  await act(async () => void fireEvent.scroll(document));
  expect(screen.queryByRole("menu")).toBeNull();
});

test("a preview answered 200 with a body that is not JSON offers no filing and posts nothing more", async () => {
  const fake = daemon(true, () => new Response("<html>proxy</html>", { status: 200 }));
  await mount("#/runs/demo/9", fake.fetchImpl);
  await revertFromRunPage();

  const dialog = screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" });
  expect(within(dialog).queryByText("Nothing merged since touches these files")).toBeNull();
  expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual(["Close"]);
  expect(fake.posted).toHaveLength(1);
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

  const dialog = screen.getByRole("alertdialog", { name: "Revert DEMO-9?" });
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
  expect(screen.queryByRole("alertdialog")).toBeNull();
  const status = document.querySelector("[data-revert-filed]")!;
  expect(status.textContent).toBe("Filed DEMO-50 to revert DEMO-9.");
  expect(within(status as HTMLElement).getByRole("link", { name: "DEMO-50" }).getAttribute("href")).toEndWith("#/tickets/demo/DEMO-50");
});

test("the filed revert's ticket link from a project-scoped Run page keeps the project scope", async () => {
  const fake = daemon(true, (body) => (body.dry_run === true ? Response.json(PREVIEW) : Response.json({ action: "revert", recorded: 42, key: "DEMO-50" })));
  await mount("#/runs/demo/9?project=demo", fake.fetchImpl);
  await revertFromRunPage();
  const dialog = screen.getByRole("alertdialog", { name: "Revert DEMO-9?" });
  await act(async () => void fireEvent.change(within(dialog).getByRole("textbox", { name: "Reason" }), { target: { value: "broke the ledger" } }));
  await act(async () => void fireEvent.click(within(dialog).getByRole("button", { name: "File revert" })));
  for (let round = 0; round < 4; round += 1) await act(settle);
  const status = document.querySelector<HTMLElement>("[data-revert-filed]")!;
  expect(within(status).getByRole("link", { name: "DEMO-50" }).getAttribute("href")).toEndWith("#/tickets/demo/DEMO-50?project=demo");
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

  const dialog = screen.getByRole("alertdialog", { name: "DEMO-9 can't be reverted" });
  expect(within(dialog).getByText("store/schema.py").tagName).toBe("CODE");
  expect(dialog.querySelector("[data-revert-refusal]")?.textContent).toBe("It changed store/schema.py, which this project never reverts. Ship a forward fix instead.");
  expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual(["Close"]);
  expect(within(dialog).queryByRole("textbox")).toBeNull();

  await act(async () => void fireEvent.click(within(dialog).getByRole("button", { name: "Close" })));
  for (let round = 0; round < 2; round += 1) await act(settle);
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(fake.posted).toHaveLength(1);
});
