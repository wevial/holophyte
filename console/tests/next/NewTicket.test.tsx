import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, Status, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, NOW, prPark, SERVE, WRITER } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const board = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const detail = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;

function fleet(answer: Fetch = async () => new Response(null, { status: 500 }), relosEditable = false): Fetch {
  const daemon = (project: string) => ({
    status: { ...working, project: `/srv/dev/${project}`, actions: true, runs: [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project: `/srv/dev/${project}`, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("demo"), [WRITER]: daemon("relos") });
  return async (url, init) => {
    if (url === `${DEMO}/tickets` || url === `${WRITER}/tickets`) return answer(url, init);
    if (url === `${DEMO}/board`) return Response.json({ ...board, editable: true });
    if (url === `${WRITER}/board`) return Response.json({ ...board, editable: relosEditable });
    if (url === `${DEMO}/tickets/DEMO-12`) return Response.json({ ...detail, ticket: "DEMO-12", title: "Steer notes show on the run detail card" });
    return polls(url);
  };
}

async function flush() {
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function mount(hash: string, fetch: Fetch = fleet()) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  await flush();
}

async function press(name: string) {
  await act(async () => void fireEvent.click(screen.getByRole("button", { name })));
  await flush();
}

const values = {
  Title: "Steer notes show on the run detail card",
  Summary: "The run page lists each steer note.",
  What: "Show the notes beside the run.",
  Why: "Keep the operator informed.",
  How: "Read the run's notes.",
  "In scope": "The run card\nIts styles",
  "Out of scope": "Editing notes",
  "Verify commands": "ruff check .",
  "Implementation notes": "Use the existing notes query.\nKeep the existing ordering.",
};

async function fill() {
  for (const [name, value] of Object.entries(values)) {
    await act(async () => void fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } }));
  }
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; });

test("Tickets offers New ticket only for editable scoped boards and the direct noneditable route explains why", async () => {
  await mount("#/tickets?project=demo");
  await press("New ticket");
  expect(window.location.hash).toBe("#/tickets/demo/new");
  expect(screen.getByRole("heading", { name: "New ticket" })).toBeTruthy();
  cleanup();

  await mount("#/tickets?project=relos");
  expect(screen.queryByRole("button", { name: "New ticket" })).toBeNull();
  cleanup();

  await mount("#/tickets/relos/new");
  expect(screen.getByText("relos takes no tickets from the console: its board is on Linear, or its host has actions off.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Back to Tickets" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "File ticket" })).toBeNull();
});

test("New ticket renders the template heading, linked breadcrumb and Summary hint on the static new route", async () => {
  await mount("#/tickets/demo/new");
  const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
  expect(crumb.textContent).toBe("Tickets / demo / new");
  expect(within(crumb).getByRole("link", { name: "Tickets" }).getAttribute("href")).toEndWith("#/tickets");
  expect(within(crumb).getByRole("link", { name: "demo" }).getAttribute("href")).toEndWith("#/tickets?project=demo");
  expect(screen.getByRole("heading", { name: "New ticket" })).toBeTruthy();
  expect(screen.getByText("demo · from the ticket template · lands in Backlog")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Summary" }).getAttribute("placeholder")).toBe("Describe the outcome in one or two sentences.");
});

test("File ticket sends one Backlog POST with the composed markdown then toasts and opens the filed ticket", async () => {
  const answer = fakeFetch(() => Response.json({ ticket: "DEMO-12", revision: 1 }, { status: 201 }));
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await fill();
  await press("File ticket");
  expect(answer.seen).toHaveLength(1);
  const request = answer.seen[0]!;
  expect([request.url, request.method]).toEqual([`${DEMO}/tickets`, "POST"]);
  const payload = request.body as { body: string; column: string };
  expect(payload.column).toBe("backlog");
  expect(payload.body).toStartWith("# Steer notes show on the run detail card\n");
  expect(payload.body).toContain("## Summary\n\nThe run page lists each steer note.");
  expect(payload.body).toContain("## In scope\n\n- The run card\n- Its styles");
  expect(payload.body).toContain("## Implementation notes\n\n- Use the existing notes query.\n- Keep the existing ordering.");
  expect(payload.body).toMatch(/```[^\n]*\nruff check \.\n```/);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Filed DEMO-12 in demo Backlog.");
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-12");
});

test("a 422 lists every problem beside File ticket with no toast and preserves all form fields", async () => {
  const answer = fakeFetch(() => Response.json({ problems: ["'Summary' is empty", "'In scope' has no entries"] }, { status: 422 }));
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await fill();
  await press("File ticket");
  const panel = screen.getByRole("button", { name: "File ticket" }).closest("aside")!;
  expect(within(panel).getByText("'Summary' is empty")).toBeTruthy();
  expect(within(panel).getByText("'In scope' has no entries")).toBeTruthy();
  expect(document.querySelector("[data-toast]")?.textContent ?? "").toBe("");
  expect(window.location.hash).toBe("#/tickets/demo/new");
  for (const [name, value] of Object.entries(values)) {
    expect((screen.getByRole("textbox", { name }) as HTMLInputElement).value).toBe(value);
  }
});

test("All projects opens the first editable project and changing the form project updates the filing destination", async () => {
  await mount("#/tickets", fleet(undefined, true));
  await press("New ticket");
  expect(window.location.hash).toBe("#/tickets/demo/new");
  const project = screen.getByRole("combobox", { name: "Project" });
  expect(within(project).getAllByRole("option").map((option) => option.textContent)).toEqual(["demo", "relos"]);
  await act(async () => void fireEvent.change(project, { target: { value: "relos" } }));
  await flush();
  expect(window.location.hash).toBe("#/tickets/relos/new");
  await fill();
  expect(screen.getByText("Files to relos Backlog.")).toBeTruthy();
});

test("All projects keeps switcher project order when its first project is editable only on a later host", async () => {
  const otherDemo = "http://writer-host:7712";
  const daemon = (project: string) => ({
    status: { ...working, project: `/srv/dev/${project}`, actions: true, runs: [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project: `/srv/dev/${project}`, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("demo"), [WRITER]: daemon("relos"), [otherDemo]: daemon("demo") });
  const answer = fakeFetch(() => Response.json({ problems: ["Complete the ticket"] }, { status: 422 }));
  const fetch: Fetch = async (url, init) => {
    if (url.endsWith("/board")) return Response.json({ ...board, columns: [], stories: [], editable: url !== `${DEMO}/board` });
    if (url.endsWith("/tickets")) return answer.fetchImpl(url, init);
    return polls(url);
  };
  await mount("#/tickets", fetch);
  await press("New ticket");
  expect(window.location.hash).toBe("#/tickets/demo/new");
  expect(within(screen.getByRole("combobox", { name: "Project" })).getAllByRole("option").map((option) => option.textContent)).toEqual(["demo", "relos"]);
  await type("Title", "Steer notes show on the run detail card");
  await press("File ticket");
  expect(answer.seen.map((request) => request.url)).toEqual([`${otherDemo}/tickets`]);
});

async function type(name: string, value: string) {
  await act(async () => void fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } }));
}

const rejectTicket = () => fakeFetch(() => Response.json({ problems: ["Complete the ticket"] }, { status: 422 }));
const posted = (answer: ReturnType<typeof fakeFetch>, index = 0) => answer.seen[index]!.body as { body: string; priority: number };

test("criteria rows reorder and remove the claims sent in the Acceptance criteria section", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  expect((screen.getByRole("textbox", { name: "AC1" }) as HTMLInputElement).value).toBe("");
  expect(screen.queryByRole("textbox", { name: "AC2" })).toBeNull();
  await press("+ Add a criterion");
  await press("+ Add a criterion");
  await type("AC1", "A");
  await type("AC2", "B");
  await type("AC3", "C");
  expect((screen.getByRole("button", { name: "Move AC1 up" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Move AC3 down" }) as HTMLButtonElement).disabled).toBe(true);
  await press("Move AC3 up");
  expect(screen.getAllByRole("textbox", { name: /^AC\d+$/ }).map(input => (input as HTMLInputElement).value)).toEqual(["A", "C", "B"]);
  await press("Remove AC1");
  expect(screen.getAllByRole("textbox", { name: /^AC\d+$/ }).map(input => (input as HTMLInputElement).value)).toEqual(["C", "B"]);
  expect(screen.getByText("2 of 2 written")).toBeTruthy();
  await press("File ticket");
  expect(posted(answer).body.split("## Acceptance criteria\n")[1]?.split("\n## ")[0]?.trim()).toBe("- [ ] C\n- [ ] B");
  await press("Remove AC2");
  await press("Remove AC1");
  expect(screen.getAllByRole("textbox", { name: /^AC\d+$/ })).toHaveLength(1);
  expect((screen.getByRole("textbox", { name: "AC1" }) as HTMLInputElement).value).toBe("");
  expect(screen.getByText("0 of 1 written")).toBeTruthy();
});

test("estimate, priority and unique uppercase dependency chips serialize and removal clears the dependency", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  expect((screen.getByRole("spinbutton", { name: "Estimate" }) as HTMLInputElement).value).toBe("25");
  expect(screen.getByRole("button", { name: "P2" }).getAttribute("aria-pressed")).toBe("true");
  for (let index = 0; index < 4; index += 1) await press("Increase estimate");
  await press("P1");
  await type("Depends on", "holo-238");
  await press("Add");
  await type("Depends on", "holo-238");
  await act(async () => void fireEvent.keyDown(screen.getByRole("textbox", { name: "Depends on" }), { key: "Enter" }));
  expect(screen.getAllByRole("button", { name: "Remove HOLO-238" })).toHaveLength(1);
  expect(screen.getByText("HOLO-238", { exact: true })).toBeTruthy();
  expect(answer.seen).toHaveLength(0);
  await press("File ticket");
  expect(posted(answer).priority).toBe(1);
  expect(posted(answer).body.match(/^Estimate:.*$/gm)?.map(line => line.trim())).toEqual(["Estimate: 45 min · Depends on: HOLO-238"]);
  await press("Remove HOLO-238");
  await press("File ticket");
  expect(posted(answer, 1).body).toContain("Estimate: 45 min · Depends on: none");
});

test("a dependency listed in the board's Ready column shows that column label", async () => {
  const polls = fleet();
  const readyBoard = {
    ...board,
    columns: board.columns.map(column => ({ ...column, tickets: column.state === "ready" ? [{ ...column.tickets[0]!, ticket: "DEMO-3" }] : [] })),
    editable: true,
  };
  await mount("#/tickets/demo/new", async (url, init) => url === `${DEMO}/board` ? Response.json(readyBoard) : polls(url, init));
  await type("Depends on", "DEMO-3");
  await press("Add");
  expect(screen.getByText("DEMO-3 · Ready", { exact: true })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Remove DEMO-3" })).toBeTruthy();
});

test.each([[true, "Needs you"], [false, "In flight"]] as const)("a pull-request dependency uses merge readiness %s for its Tickets column label", async (ready, label) => {
  const polls = fleet();
  const merge = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();
  const parkedBoard = {
    ...board,
    columns: board.columns.map(column => ({ ...column, tickets: column.state === "blocked_on_operator" ? [{ ...column.tickets[0]!, ticket: "DEMO-9" }] : [] })),
    editable: true,
  };
  await mount("#/tickets/demo/new", async (url, init) => {
    if (url === `${DEMO}/board`) return Response.json(parkedBoard);
    if (url === `${DEMO}/attention`) return Response.json({ level: "attention", now: NOW, project: "/srv/dev/demo", items: [{ ...prPark, ticket: "DEMO-9" }] });
    if (url === `${DEMO}/runs/9/merge`) return Response.json({ ...merge, run: 9, ticket: "DEMO-9", ready });
    return polls(url, init);
  });
  await type("Depends on", "DEMO-9");
  await press("Add");
  expect(screen.getByText(`DEMO-9 · ${label}`, { exact: true })).toBeTruthy();
});

test("UI change serializes its major declaration, mock-up and approval and switching off removes them", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  const toggle = screen.getByRole("switch", { name: "This ticket changes the UI" });
  expect(screen.queryByRole("textbox", { name: "Mock-up link" })).toBeNull();
  await act(async () => void fireEvent.click(toggle));
  await type("Mock-up link", "https://claude.ai/artifact/abc");
  await type("Boards", "https://example.test/board.png");
  const approval = (screen.getByRole("textbox", { name: "Approval" }) as HTMLInputElement).value;
  expect(approval).toMatch(/^Approved \d{4}-\d{2}-\d{2}:/);
  await press("File ticket");
  const body = posted(answer).body;
  expect(body).toContain("**UI change:** major");
  const mockup = body.split("## Mock-up\n")[1]?.split("\n## ")[0];
  expect(mockup).toMatch(/^https:\/\/claude\.ai\/artifact\/abc\s*$/m);
  expect(body.match(/^Approved .+$/gm)).toHaveLength(1);
  expect(body).toContain("![board](https://example.test/board.png)");
  await act(async () => void fireEvent.click(toggle));
  await press("File ticket");
  const without = posted(answer, 1).body;
  expect(without).not.toContain("**UI change:**");
  expect(without).not.toContain("## Mock-up");
  expect(without).not.toContain("https://claude.ai/artifact/abc");
  expect(without).not.toContain("https://example.test/board.png");
});

test("estimate stepping stops at 5 and 120 minutes and files those boundary values", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  for (let index = 0; index < 5; index += 1) await press("Decrease estimate");
  expect((screen.getByRole("spinbutton", { name: "Estimate" }) as HTMLInputElement).value).toBe("5");
  await press("File ticket");
  expect(posted(answer).body).toContain("Estimate: 5 min · Depends on: none");
  await act(async () => void fireEvent.change(screen.getByRole("spinbutton", { name: "Estimate" }), { target: { value: "115" } }));
  await press("Increase estimate");
  await press("Increase estimate");
  expect((screen.getByRole("spinbutton", { name: "Estimate" }) as HTMLInputElement).value).toBe("120");
  await press("File ticket");
  expect(posted(answer, 1).body).toContain("Estimate: 120 min · Depends on: none");
});


test("typing an estimate digit by digit preserves the intended number when filing", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  const user = userEvent.setup();
  const estimate = screen.getByRole("spinbutton", { name: "Estimate" });
  await act(async () => {
    await user.clear(estimate);
    await user.type(estimate, "45");
  });
  expect((estimate as HTMLInputElement).value).toBe("45");
  await act(async () => void await user.click(screen.getByRole("button", { name: "File ticket" })));
  await flush();
  expect(posted(answer).body.match(/^Estimate:.*$/gm)?.map(line => line.trim())).toEqual(["Estimate: 45 min · Depends on: none"]);
});
