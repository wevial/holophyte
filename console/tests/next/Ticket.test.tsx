import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardColumn, BoardTicket, RunCriterion, RunDetail, Status, StoryDetail, TicketDetail, TicketRun } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, run, SERVE } from "./needsFleet";

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const servedStory = (await Bun.file(new URL("story.json", SERVE)).json()) as StoryDetail;

const boardTicket = (key: string, facts: Partial<BoardTicket>): BoardTicket => ({ ...template, ticket: key, title: `${key} title`, ...facts });
const column = (state: BoardColumn["state"], tickets: BoardTicket[]): BoardColumn => ({ state, tickets });

const board: Board = {
  ...pinned,
  stories: [
    { ticket: "DEMO-20", title: "Unattended runs recover themselves", children: ["DEMO-21", "DEMO-22", "DEMO-23"], merged: 1, total: 3 },
    { ticket: "DEMO-30", title: "An empty story", children: [], merged: 0, total: 0 },
  ],
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
  runs: [],
  ticket: key,
  title: `${key} title`,
  body: `# ${key} title\n\n## Summary\n\n${SUMMARY}\n\n## What / Why / How\n\n**What:** a cache.\n`,
  revisions: [
    { revision: 2, at: FILED_AT + 60 * MINUTE, author: "operator" },
    { revision: 1, at: FILED_AT, author: "maintainer" },
  ],
  ...facts,
});

const LATER = new Date(2026, 9, 15, 17, 0).getTime();
const servedRunRow = servedTicket.runs[0]!;
const runRow = (facts: Partial<TicketRun>): TicketRun => ({ ...servedRunRow, ...facts });
const DEMO_3_RUNS = [
  runRow({ run: 5, attempt: 1, phase: "failed", outcome: "failed", rounds: 1, pr_url: null, started_ms: new Date(2026, 9, 14, 15, 10).getTime() }),
  runRow({ run: 9, attempt: 2, phase: "reviewing", outcome: null, rounds: 1, pr_url: "https://github.com/acme/demo/pull/524", started_ms: new Date(2026, 9, 15, 16, 36).getTime() }),
];

const child = (ticket: string, status: StoryDetail["children"][number]["status"]) => ({ ...servedStory.children[0]!, ticket, title: `${ticket} title`, status, column: null });
const stories: Record<string, StoryDetail> = {
  "DEMO-20": { ...servedStory, ticket: "DEMO-20", title: "Unattended runs recover themselves", children: [child("DEMO-21", "merged"), child("DEMO-22", "ready"), child("DEMO-23", "in_flight")] },
  "DEMO-30": { ...servedStory, ticket: "DEMO-30", title: "An empty story", children: [] },
};

const tickets: Record<string, TicketDetail> = {
  "DEMO-3": detail("DEMO-3", { run: 9, time_box_ms: 25 * MINUTE, acceptance_criteria: CRITERIA, criteria: CRITERIA.map((text, index) => ({ id: `C${index + 1}`, text })), verification_commands: COMMANDS, runs: DEMO_3_RUNS }),
  "DEMO-2": detail("DEMO-2", { run: null, status: "ready" }),
  "DEMO-20": detail("DEMO-20", { run: null, status: "ready" }),
  "DEMO-22": detail("DEMO-22", { run: null, status: "ready" }),
  "DEMO-30": detail("DEMO-30", { run: null, status: "ready" }),
  "DEMO-5": detail("DEMO-5", { run: null, status: "blocked_on_deps", acceptance_criteria: CRITERIA.slice(0, 2), criteria: CRITERIA.slice(0, 2).map((text, index) => ({ id: `C${index + 1}`, text })) }),
  "DEMO-10": detail("DEMO-10", { run: null, status: "merged" }),
  "DEMO-6": detail("DEMO-6", { run: null, status: "abandoned", current: { ...servedTicket.current!, column: "canceled" } }),
};

const judged = servedRun.criteria[0]!;
const standing = (n: number, status: RunCriterion["status"]): RunCriterion => ({ ...judged, id: `C${n}`, n, text: CRITERIA[n - 1]!, status, run: 9 });
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
    const story = url.startsWith(`${DEMO}/stories/`) ? decodeURIComponent(url.slice(`${DEMO}/stories/`.length)) : null;
    if (story != null) return story in stories ? Response.json(stories[story]) : new Response("not found", { status: 404 });
    if (url === `${DEMO}/runs/9`) return Response.json({ ...servedRun, run: { ...servedRun.run, id: 9, ticket: "DEMO-3" }, criteria });
    if (url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  };
}

let requested: string[] = [];

async function mount(hash: string, runAnswer?: () => Response, now = NOW) {
  window.location.hash = hash;
  const answer = ticketFetch(runAnswer);
  requested = [];
  const fake = fakeDeps((url, init) => {
    requested.push(url);
    return answer(url, init);
  });
  fake.clock.now = now;
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
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
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
  expect(within(list).getAllByRole("listitem").map((row) => row.textContent)).toEqual(CRITERIA.map((text, index) => `C${index + 1}${text}`));
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

const story = () => within(facts()).getByRole("region", { name: "Story" });

test("the Runs table lists every attempt newest first with its outcome, rounds, pull request and start, each linking to the classic run view", async () => {
  await mount("#/tickets/demo/DEMO-3", undefined, LATER);
  const section = screen.getByRole("region", { name: "Runs 2" });
  const rows = within(section).getAllByRole("row").slice(1);
  expect(rows.map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent).join(" · "))).toEqual([
    "run 9 · reviewing · 2 of 3 · PR #524 · today 16:36",
    "run 5 · failed · 1 · – · yesterday 15:10",
  ]);
  expect(within(section).getByRole("link", { name: "run 9" }).getAttribute("href")).toBe(`/?classic#run=9&daemon=${encodeURIComponent(DEMO)}`);
  expect(within(section).getByRole("link", { name: "run 5" }).getAttribute("href")).toBe(`/?classic#run=5&daemon=${encodeURIComponent(DEMO)}`);
  expect(within(section).getByRole("link", { name: "PR #524" }).getAttribute("href")).toBe("https://github.com/acme/demo/pull/524");
});

test("a ticket no run has claimed counts no runs and says so", async () => {
  await mount("#/tickets/demo/DEMO-2");
  const section = screen.getByRole("region", { name: "Runs 0" });
  expect(within(section).queryByRole("table")).toBeNull();
  expect(within(section).getByText("No run has claimed DEMO-2 yet.")).toBeTruthy();
});

test("a story's child shows its parent, the story's progress and every sibling's state with itself unlinked", async () => {
  await mount("#/tickets/demo/DEMO-22");
  const panel = story();
  expect(within(panel).getByText("Parent")).toBeTruthy();
  const parent = within(panel).getByRole("link", { name: "DEMO-20 Unattended runs recover themselves" });
  expect(parent.getAttribute("href")).toEndWith("#/tickets/demo/DEMO-20");
  expect(within(panel).getByText("1 of 3 merged")).toBeTruthy();
  const rows = within(panel).getAllByRole("listitem");
  expect(rows.map((row) => within(row).getByRole("img").getAttribute("aria-label"))).toEqual(["merged", "queued", "running"]);
  expect(rows.map((row) => row.textContent)).toEqual(["DEMO-21 DEMO-21 title merged", "DEMO-22 DEMO-22 title ready · this ticket", "DEMO-23 DEMO-23 title in flight"]);
  expect(within(rows[0]!).getByRole("link", { name: "DEMO-21" }).getAttribute("href")).toEndWith("#/tickets/demo/DEMO-21");
  expect(within(rows[1]!).queryByRole("link")).toBeNull();
});

test("a story's own page heads its children, an empty story says it has none, and a ticket in no story draws no panel nor asks for one", async () => {
  await mount("#/tickets/demo/DEMO-20");
  expect(within(story()).getByText("Child tickets")).toBeTruthy();
  expect(within(story()).queryByText("Parent")).toBeNull();
  expect(within(story()).getAllByRole("listitem")).toHaveLength(3);
  cleanup();
  await mount("#/tickets/demo/DEMO-30");
  expect(within(story()).getByText("No child tickets.")).toBeTruthy();
  cleanup();
  await mount("#/tickets/demo/DEMO-2");
  expect(within(facts()).queryByRole("region", { name: "Story" })).toBeNull();
  expect(requested.some((url) => url.includes("/stories/"))).toBe(false);
  expect(requested).toContain(`${DEMO}/board`);
});
