import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, Status, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, peersFetch, settle } from "../harness";
import { DEMO, NOW, SERVE, WRITER } from "./needsFleet";

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

test("All projects opens the first editable project and changing the form project retains the fields", async () => {
  await mount("#/tickets", fleet(undefined, true));
  await press("New ticket");
  expect(window.location.hash).toBe("#/tickets/demo/new");
  await fill();
  const project = screen.getByRole("combobox", { name: "Project" });
  expect(within(project).getAllByRole("option").map((option) => option.textContent)).toEqual(["demo", "relos"]);
  await act(async () => void fireEvent.change(project, { target: { value: "relos" } }));
  await flush();
  expect(window.location.hash).toBe("#/tickets/relos/new");
  expect(screen.getByText("Files to relos Backlog.")).toBeTruthy();
  for (const [name, value] of Object.entries(values)) {
    expect((screen.getByRole("textbox", { name }) as HTMLInputElement).value).toBe(value);
  }
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
  await press("File ticket");
  expect(answer.seen.map((request) => request.url)).toEqual([`${otherDemo}/tickets`]);
});
