import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, Status, TicketCheck } from "../../src/lib/contract.gen";
import type { Fetch, PollDeps } from "../../src/lib/poll";
import { placeMessage } from "../../src/next/checker";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, NOW, SERVE } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const board = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const pinned = (await Bun.file(new URL("ticket-check.json", SERVE)).json()) as TicketCheck;
const OPEN_QUESTIONS = "'Open questions' must read exactly '- None' before the ticket enters the pickable queue";

function fleet(check: Fetch): Fetch {
  const polls = peersFetch(DEMO, {
    [DEMO]: {
      status: { ...working, project: "/srv/dev/demo", actions: true, runs: [], queued: [], now: NOW },
      attention: { level: "none", now: NOW, project: "/srv/dev/demo", items: [] },
    },
  });
  return async (url, init) => {
    if (url === `${DEMO}/tickets/check`) return check(url, init);
    if (url === `${DEMO}/board`) return Response.json({ ...board, editable: true });
    return polls(url);
  };
}

async function flush() {
  for (let round = 0; round < 4; round += 1) await act(settle);
}

/** A clock for the page's timers: `advance(ms)` runs each one that falls due. */
let advance: (ms: number) => Promise<void>;

async function mount(check: Fetch) {
  window.location.hash = "#/tickets/demo/new";
  const fake = fakeDeps(fleet(check));
  let now = 0;
  const timers: { at: number; fn: () => void }[] = [];
  const deps: PollDeps = {
    ...fake.deps,
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
    for (const entry of timers.filter(timer => timer.at <= now)) {
      const at = timers.indexOf(entry);
      if (at < 0) continue;
      timers.splice(at, 1);
      await act(async () => entry.fn());
    }
    await flush();
  };
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={deps} /></QueryClientProvider>);
  await flush();
}

async function type(name: string, value: string) {
  await act(async () => void fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } }));
}

const panel = () => screen.getByRole("region", { name: "Checker" });
const fileButton = () => screen.getByRole("button", { name: "File ticket" }) as HTMLButtonElement;
/** The messages shown under the field `element` names as its description. */
const under = (element: HTMLElement) => (element.getAttribute("aria-describedby") ?? "").split(" ").map(id => document.getElementById(id)?.textContent ?? "").join("\n");
const inline = () => [...document.querySelectorAll("[id^='check-']")].map(element => element.textContent).join("\n");

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; });

test("the pinned judgement lists in the panel, shows each message under its field and holds File ticket", async () => {
  const check = fakeFetch(pinned);
  await mount(check.fetchImpl);
  await type("Title", "Steer notes show on the run detail card");
  await type("Summary", "The run page lists each steer note.");
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "+ Add a criterion" })));
  await act(async () => void fireEvent.click(screen.getByRole("button", { name: "+ Add a criterion" })));
  for (const [name, value] of [["AC1", "Notes show"], ["AC2", "Authors show"], ["AC3", "Times show"]]) await type(name!, value!);
  await advance(599);
  expect(check.seen).toHaveLength(0);
  await advance(1);
  expect(check.seen).toHaveLength(1);
  const request = check.seen[0]!;
  expect([request.url, request.method]).toEqual([`${DEMO}/tickets/check`, "POST"]);
  expect(request.body).toMatchObject({ column: "backlog" });
  expect((request.body as { body: string }).body).toStartWith("# Steer notes show on the run detail card\n");

  expect(within(panel()).getByText("3 errors block filing · 1 advisory")).toBeTruthy();
  const summary = screen.getByRole("textbox", { name: "Summary" });
  expect(summary.getAttribute("aria-invalid")).toBe("true");
  expect(under(summary)).toContain("'Summary' is empty");
  expect(under(screen.getByRole("textbox", { name: "AC2" }))).toContain("acceptance criterion #2 is empty");
  expect(under(screen.getByRole("spinbutton", { name: "Estimate" }))).toContain("estimate is 120 min; the cap is 90 min — split the ticket");
  const notes = screen.getByRole("textbox", { name: "Implementation notes" });
  expect(under(notes)).toContain("path does not exist in Implementation notes: holophyte/nope_missing.py");
  expect(notes.getAttribute("aria-invalid")).toBeNull();
  expect(fileButton().disabled).toBe(true);
  expect(screen.getByText("Fix 3 errors to file.")).toBeTruthy();

  await act(async () => void fireEvent.click(within(panel()).getByRole("button", { name: /^Summary/ })));
  expect(document.activeElement).toBe(summary);
});

test("edits 200 ms apart send one check 600 ms after the last, and a late answer for an older body is dropped", async () => {
  let release!: () => void;
  const older = fakeFetch(pinned, new Promise<void>(resolve => { release = resolve; }));
  const newer = fakeFetch({ problems: [], advisories: [], blocks: false });
  let calls = 0;
  await mount((url, init) => (calls++ === 0 ? older : newer).fetchImpl(url, init));
  await type("Title", "One");
  await advance(200);
  await type("Title", "One two");
  await advance(200);
  await type("Title", "One two three");
  await advance(599);
  expect(older.seen).toHaveLength(0);
  await advance(1);
  expect(older.seen.map(request => (request.body as { body: string }).body.split("\n")[0])).toEqual(["# One two three"]);

  await type("Title", "One two three four");
  await advance(600);
  expect(newer.seen).toHaveLength(1);
  expect(within(panel()).getByText("Ready. Nothing to fix.")).toBeTruthy();
  release();
  await flush();
  expect(within(panel()).getByText("Ready. Nothing to fix.")).toBeTruthy();
  expect(within(panel()).queryByText(/errors block filing/)).toBeNull();
  expect(fileButton().disabled).toBe(false);
});

test("advisories alone leave File ticket ready and a message no field owns shows only in the panel under Ticket", async () => {
  let answer: TicketCheck = { problems: [], advisories: [pinned.advisories[0]!], blocks: false };
  await mount(fakeFetch(() => Response.json(answer)).fetchImpl);
  await type("Title", "Steer notes show on the run detail card");
  await advance(600);
  expect(within(panel()).getByText("Ready · 1 advisory, none blocking")).toBeTruthy();
  expect(fileButton().disabled).toBe(false);
  expect(screen.getByText("Files to demo Backlog.")).toBeTruthy();

  answer = { problems: [OPEN_QUESTIONS], advisories: [], blocks: true };
  await type("Summary", "The run page lists each steer note, newest first.");
  await advance(600);
  const row = within(panel()).getByText(OPEN_QUESTIONS).closest("li")!;
  expect(row.textContent).toStartWith("×Ticket");
  expect(inline()).not.toContain(OPEN_QUESTIONS);
  expect(screen.getByText("Fix 1 error to file.")).toBeTruthy();
});

test("a blank form sends no check and holds File ticket; a host without the route leaves it enabled", async () => {
  const check = fakeFetch(() => Response.json({ error: "not found" }, { status: 404 }));
  await mount(check.fetchImpl);
  await advance(600);
  expect(within(panel()).getByText("Nothing to check yet. Start with a title.")).toBeTruthy();
  expect(check.seen).toHaveLength(0);
  expect(fileButton().disabled).toBe(true);
  expect(screen.getByText("Fill in the template to file.")).toBeTruthy();

  await type("Title", "Steer notes show on the run detail card");
  await advance(600);
  expect(check.seen).toHaveLength(1);
  expect(within(panel()).getByText("This host does not check as you type; File ticket still checks.")).toBeTruthy();
  expect(fileButton().disabled).toBe(false);
});

test("checker messages land on the field their wording names, the rest on the ticket", () => {
  expect([
    "missing H1 title ('# ...' on the first heading line)",
    "'**How:**' line missing under 'What / Why / How' ('Why' is optional)",
    "criterion 3 reads as an operator or post-merge witness; the reviewer can only witness the candidate branch",
    "path does not exist in Acceptance criteria #4: holophyte/nope.py",
    "'Acceptance criteria' has no '- [ ] Given/when/then' items",
    "'Estimate & dependencies' must read 'Estimate: N min · Depends on: <ticket IDs or \"none\">'",
    "'Depends on' names no ticket of this project: DEMO-99",
    "unittest module does not exist in verify command: tests/test_nope.py",
    "'## Mock-up' has no link to the approved mock-up",
    OPEN_QUESTIONS,
  ].map(placeMessage)).toEqual(["Title", "How", "AC3", "AC4", "Acceptance criteria", "Estimate", "Depends on", "Verify commands", "Mock-up link", "Ticket"]);
});
