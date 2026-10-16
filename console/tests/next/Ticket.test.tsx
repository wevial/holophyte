import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, BoardTicket, RunCriterion, RunDetail, Status, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, run, SERVE } from "./needsFleet";

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;

const boardTicket = (key: string, facts: Partial<BoardTicket>): BoardTicket => ({ ...template, ticket: key, title: `${key} title`, ...facts });
const column = (state: BoardColumn["state"], tickets: BoardTicket[]): BoardColumn => ({ state, tickets });

const board: Board = {
  ...pinned,
  columns: [
    column("blocked_on_deps", [boardTicket("DEMO-5", { run: null, waits_on: ["DEMO-2"] })]),
    column("in_flight", [boardTicket("DEMO-3", { time_box_ms: 25 * MINUTE, run: 9 })]),
  ],
};

const SUMMARY = "The board query reads every ticket on each poll. Cache it for one poll interval.";
const CRITERIA = ["Two polls inside the interval read the store once.", "A ticket write clears the cache.", "The cache holds one board per project."];
const COMMANDS = ["ruff check .", "python3 -m unittest discover -s tests -p 'test_board.py'"];
const FILED_AT = new Date(2026, 9, 13, 14, 52).getTime();

const detail = (key: string, facts: Partial<TicketDetail>): TicketDetail => ({
  ...servedTicket,
  ticket: key,
  title: `${key} title`,
  body: `# ${key} title\n\n## Summary\n\n${SUMMARY}\n\n## What / Why / How\n\n**What:** a cache.\n`,
  revisions: [
    { revision: 2, at: FILED_AT + 60 * MINUTE, author: "operator" },
    { revision: 1, at: FILED_AT, author: "maintainer" },
  ],
  ...facts,
});

const tickets: Record<string, TicketDetail> = {
  "DEMO-3": detail("DEMO-3", { run: 9, time_box_ms: 25 * MINUTE, acceptance_criteria: CRITERIA, verification_commands: COMMANDS }),
  "DEMO-5": detail("DEMO-5", { run: null, status: "blocked_on_deps", acceptance_criteria: CRITERIA.slice(0, 2) }),
  "DEMO-10": detail("DEMO-10", { run: null, status: "merged" }),
  "DEMO-6": detail("DEMO-6", { run: null, status: "abandoned", current: { ...servedTicket.current!, column: "canceled" } }),
};

const judged = servedRun.criteria[0]!;
const standing = (n: number, status: RunCriterion["status"]): RunCriterion => ({ ...judged, n, text: CRITERIA[n - 1]!, status, run: 9 });
const criteria = [standing(1, "met"), standing(2, "met"), standing(3, "unwitnessed")];

/** DEMO serves the board above, runs DEMO-3 as run 9 reviewing round 2 of 3
 *  and mirrors `tickets`; WRITER, of another project, mirrors nothing. */
function ticketFetch(runAnswer?: () => Response): Fetch {
  const polls = fleet({ items: [], runs: [{ ...run(9, "DEMO-3"), phase: "reviewing", round: 1, max_rounds: 3 }] }, { items: [] });
  return async (url) => {
    if (url === `${DEMO}/status`) {
      const status = (await (await polls(url)).json()) as Status;
      return Response.json({ ...status, route_labels: { ...status.route_labels, implementer: "claude opus", reviewer: "codex gpt-6-sol" } });
    }
    if (url === `${DEMO}/board`) return Response.json(board);
    const key = url.startsWith(`${DEMO}/tickets/`) ? decodeURIComponent(url.slice(`${DEMO}/tickets/`.length)) : null;
    if (key != null) return key in tickets ? Response.json(tickets[key]) : new Response("not found", { status: 404 });
    if (url === `${DEMO}/runs/9` && runAnswer != null) return runAnswer();
    if (url === `${DEMO}/runs/9`) return Response.json({ ...servedRun, run: { ...servedRun.run, id: 9, ticket: "DEMO-3" }, criteria });
    if (url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  };
}

async function mount(hash: string, runAnswer?: () => Response) {
  window.location.hash = hash;
  const fake = fakeDeps(ticketFetch(runAnswer));
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const facts = () => screen.getByRole("complementary", { name: "Ticket facts and story" });
const fact = (label: string) => within(facts()).getByText(label, { selector: "dt" }).nextElementSibling as HTMLElement;
const chip = () => screen.getByRole("status", { name: "Board" }).textContent;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("an in-flight ticket's page has its breadcrumb, title, board chip with run and phase, and a link to the classic run view", async () => {
  await mount("#/tickets/demo/DEMO-3");
  const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
  expect(crumb.textContent).toBe("Tickets / demo tickets / DEMO-3");
  expect(within(crumb).getByRole("link", { name: "Tickets" }).getAttribute("href")).toEndWith("#/tickets");
  expect(within(crumb).getByRole("link", { name: "demo tickets" }).getAttribute("href")).toEndWith("#/tickets?project=demo");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-3 title");
  expect(chip()).toBe("In flight · run 9 · reviewing · round 2 of 3");
  expect(screen.getByRole("link", { name: "open run 9" }).getAttribute("href")).toBe(`/?classic#run=9&daemon=${encodeURIComponent(DEMO)}`);
});

test("the panel shows the summary, keyed criteria counted in their run, the folded verify commands and the ticket's facts", async () => {
  await mount("#/tickets/demo/DEMO-3");
  expect(screen.getByText(SUMMARY)).toBeTruthy();
  const list = screen.getByRole("region", { name: "Acceptance criteria" });
  expect(list.querySelector("[data-met-count]")!.textContent).toBe("2 of 3 met in run 9");
  expect(within(list).getAllByRole("listitem").map((row) => row.textContent)).toEqual(CRITERIA.map((text, index) => `AC${index + 1}${text}`));
  expect(Array.from(list.querySelectorAll("[data-standing]")).map((mark) => mark.getAttribute("data-standing"))).toEqual(["met", "met", "open"]);

  const verify = screen.getByText("Verify").closest("details")!;
  expect(verify.open).toBe(false);
  expect(within(verify).getByText("2 commands")).toBeTruthy();
  await act(async () => void fireEvent.click(within(verify).getByText("Verify")));
  expect(verify.open).toBe(true);
  expect(verify.querySelector("pre")!.textContent).toBe(COMMANDS.join("\n"));

  expect(fact("Project").textContent).toBe("demo");
  expect(within(fact("Project")).getByRole("link").getAttribute("href")).toEndWith("#/tickets?project=demo");
  expect(fact("Time box").textContent).toBe("25m");
  expect(fact("Implementer").textContent).toBe("claude opus");
  expect(fact("Reviewer").textContent).toBe("codex gpt-6-sol");
  expect(fact("Filed").textContent).toEndWith(" by maintainer");
  expect(within(facts()).queryByText("Waits on")).toBeNull();
});

test("a run whose criteria have not answered withholds the met count and every standing", async () => {
  await mount("#/tickets/demo/DEMO-3", () => new Response("down", { status: 500 }));
  const list = screen.getByRole("region", { name: "Acceptance criteria" });
  expect(list.querySelector("[data-met-count]")!.textContent).toBe("run 9 unavailable");
  expect(list.querySelectorAll("[data-standing]").length).toBe(0);
  expect(within(list).getAllByRole("listitem")).toHaveLength(3);
});

test("a ticket waiting on another with no run reads blocked, counts criteria with no run and names what it waits on", async () => {
  await mount("#/tickets/demo/DEMO-5");
  expect(chip()).toBe("Ready · blocked by DEMO-2");
  expect(screen.queryByRole("link", { name: /^open run/ })).toBeNull();
  expect(document.querySelector("[data-met-count]")!.textContent).toBe("0 of 2 met");
  expect(fact("Waits on").textContent).toBe("DEMO-2");
});

test("a merged ticket and a cancelled one, neither on the board, take their chips from their own status", async () => {
  await mount("#/tickets/demo/DEMO-10");
  expect(chip()).toBe("Merged");
  cleanup();
  await mount("#/tickets/demo/DEMO-6");
  expect(chip()).toBe("Cancelled");
});

test("a ticket no host of the project mirrors says so with a link to Tickets, and an unknown project is not found", async () => {
  await mount("#/tickets/demo/DEMO-99");
  expect(screen.getByText("No host of demo has mirrored DEMO-99.").classList.contains("font-mono")).toBe(true);
  expect(screen.getByRole("link", { name: "Back to Tickets" }).getAttribute("href")).toEndWith("#/tickets?project=demo");
  cleanup();
  await mount("#/tickets/nope/X-1");
  expect(screen.getByText("There is no page here.")).toBeTruthy();
});

test("Expand in the peek opens the ticket's page", async () => {
  await mount("#/tickets");
  await act(async () => void fireEvent.click(document.getElementById("card-demo-DEMO-3")!));
  await act(async () => void fireEvent.click(within(screen.getByRole("dialog")).getByRole("link", { name: /^Expand/ })));
  for (let round = 0; round < 6; round += 1) await act(settle);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-3 title");
  expect(chip()).toBe("In flight · run 9 · reviewing · round 2 of 3");
  const pages = within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("link");
  expect(pages.filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.getAttribute("aria-label"))).toEqual(["Tickets"]);
});
