import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, Status, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
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
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  await flush();
  return fake;
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
beforeAll(() => happyDOM.setURL(`${DEMO}/`));
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

test("New ticket heads the page with the title alone, a linked breadcrumb and the Summary hint, with no subtitle or footer", async () => {
  await mount("#/tickets/demo/new");
  const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
  expect(crumb.textContent).toBe("Tickets / demo / new");
  expect(within(crumb).getByRole("link", { name: "Tickets" }).getAttribute("href")).toEndWith("#/tickets");
  expect(within(crumb).getByRole("link", { name: "demo" }).getAttribute("href")).toEndWith("#/tickets?project=demo");
  const page = screen.getByRole("region", { name: "New ticket" });
  expect(page.querySelector("header")!.textContent).toBe("New ticket");
  expect(within(page).queryByText(/from the ticket template|lands in Backlog/)).toBeNull();
  expect(page.querySelector("footer")).toBeNull();
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
  expect(payload.body).toStartWith('+++\nui_change = "none"\nestimate = 25\ndepends_on = []\n+++\n# Steer notes show on the run detail card\n');
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

test("All projects opens the first editable project and picking another project in the Project picker updates the filing destination", async () => {
  await mount("#/tickets", fleet(undefined, true));
  await press("New ticket");
  expect(window.location.hash).toBe("#/tickets/demo/new");
  expect(document.querySelector("select")).toBeNull();
  expect(await projectOptions()).toEqual(["demo", "relos"]);
  await pickProject("relos");
  expect(window.location.hash).toBe("#/tickets/relos/new");
  await fill();
  expect(screen.getByText("Files to relos Backlog.")).toBeTruthy();
});

test("the Project picker lists only editable projects in switcher order and filing after a pick posts to the picked project", async () => {
  const [zeta, alpha, mid] = ["http://writer-host:7720", "http://writer-host:7721", "http://writer-host:7722"];
  const daemon = (project: string) => ({
    status: { ...working, project: `/srv/dev/${project}`, actions: true, runs: [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project: `/srv/dev/${project}`, items: [] },
  });
  const polls = peersFetch(zeta, { [zeta]: daemon("zeta"), [alpha]: daemon("alpha"), [mid]: daemon("mid") });
  const answer = fakeFetch(() => Response.json({ problems: ["Complete the ticket"] }, { status: 422 }));
  const fetch: Fetch = async (url, init) => {
    if (url.endsWith("/board")) return Response.json({ ...board, columns: [], stories: [], editable: !url.startsWith(alpha) });
    if (url.endsWith("/tickets")) return answer.fetchImpl(url, init);
    return polls(url);
  };
  window.location.hash = "#/tickets/zeta/new";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><ConsoleApp base={zeta} pollDeps={fake.deps} /></QueryClientProvider>);
  await flush();
  expect(await projectOptions()).toEqual(["zeta", "mid"]);
  await pickProject("mid");
  expect(window.location.hash).toBe("#/tickets/mid/new");
  await type("Title", "Steer notes show on the run detail card");
  await press("File ticket");
  expect(answer.seen.map((request) => request.url)).toEqual([`${mid}/tickets`]);
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
  expect(await projectOptions()).toEqual(["demo", "relos"]);
  await type("Title", "Steer notes show on the run detail card");
  await press("File ticket");
  expect(answer.seen.map((request) => request.url)).toEqual([`${otherDemo}/tickets`]);
});

async function openProject() {
  await act(async () => void fireEvent.click(screen.getByLabelText("Project", { selector: "button" })));
  await flush();
  return screen.getByRole("listbox", { name: "Project" });
}

async function projectOptions() {
  const options = within(await openProject()).getAllByRole("option").map((option) => option.textContent);
  await act(async () => void fireEvent.keyDown(document.activeElement!, { key: "Escape" }));
  await flush();
  return options;
}

async function pickProject(name: string) {
  await act(async () => void fireEvent.click(within(await openProject()).getByRole("option", { name })));
  await flush();
}

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
  const priority = screen.getByRole("group", { name: "Priority" });
  expect(within(priority).getAllByRole("button").map(button => [button.textContent, button.getAttribute("aria-pressed")]))
    .toEqual([["None", "false"], ["P1", "false"], ["P2", "true"], ["P3", "false"], ["P4", "false"]]);
  for (let index = 0; index < 4; index += 1) await press("Increase estimate");
  await press("P1");
  expect(within(priority).getByRole("button", { name: "P1" }).getAttribute("aria-pressed")).toBe("true");
  await type("Depends on", "holo-238");
  await press("Add");
  await type("Depends on", "holo-238");
  await act(async () => void fireEvent.keyDown(screen.getByRole("textbox", { name: "Depends on" }), { key: "Enter" }));
  expect(screen.getAllByRole("button", { name: "Remove HOLO-238" })).toHaveLength(1);
  expect(screen.getByText("HOLO-238", { exact: true })).toBeTruthy();
  expect(answer.seen).toHaveLength(0);
  await press("File ticket");
  expect(posted(answer).priority).toBe(1);
  expect(posted(answer).body.match(/^(?:estimate|depends_on) = .*$/gm)).toEqual(["estimate = 45", 'depends_on = ["HOLO-238"]']);
  await press("Remove HOLO-238");
  await press("File ticket");
  expect(posted(answer, 1).body.match(/^(?:estimate|depends_on) = .*$/gm)).toEqual(["estimate = 45", "depends_on = []"]);
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

const picker = (name: string) => screen.getByLabelText(name, { selector: "button" });
async function open(name: string) {
  await act(async () => void fireEvent.click(picker(name)));
  await flush();
  return screen.getByRole("listbox", { name });
}
async function options(name: string) {
  const list = await open(name);
  const shown = within(list).getAllByRole("option").map(option => [option.textContent, option.getAttribute("aria-selected")]);
  await act(async () => void fireEvent.keyDown(list.parentElement!, { key: "Escape" }));
  await flush();
  return shown;
}
async function choose(name: string, label: string) {
  const list = await open(name);
  await act(async () => void fireEvent.click(within(list).getByRole("option", { name: label })));
  await flush();
}
const header = (body: string) => body.slice(0, body.indexOf("\n+++\n") + 4).split("\n");

test("UI change chooses None, Minor or Major: Minor files no [mockup], Major files its link, approval date, note and boards", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  expect(picker("UI change").textContent).toBe("None");
  expect(await options("UI change")).toEqual([["None", "true"], ["Minor", "false"], ["Major", "false"]]);
  await act(async () => void fireEvent.change(screen.getByRole("spinbutton", { name: "Estimate" }), { target: { value: "45" } }));
  await type("Depends on", "HOLO-7");
  await press("Add");

  await choose("UI change", "Minor");
  expect(screen.queryByRole("textbox", { name: "Mock-up link" })).toBeNull();
  await press("File ticket");
  expect(header(posted(answer).body)).toEqual([
    "+++",
    'ui_change = "minor"',
    "estimate = 45",
    'depends_on = ["HOLO-7"]',
    "+++",
  ]);

  await choose("UI change", "Major");
  expect(picker("UI change").textContent).toBe("Major");
  await type("Mock-up link", "https://claude.ai/artifact/abc123");
  await act(async () => void fireEvent.change(screen.getByLabelText("Approved on"), { target: { value: "2026-10-19" } }));
  await type("What was approved", "the defaults");
  await type("Boards", "https://media.example/b.png");
  await press("File ticket");
  const body = posted(answer, 1).body;
  expect(header(body)).toEqual([
    "+++",
    'ui_change = "major"',
    "estimate = 45",
    'depends_on = ["HOLO-7"]',
    "",
    "[mockup]",
    'url = "https://claude.ai/artifact/abc123"',
    "approved = 2026-10-19",
    'note = "the defaults"',
    "",
    "[[mockup.boards]]",
    'url = "https://media.example/b.png"',
    "+++",
  ]);
  expect(body).not.toMatch(/^\*\*UI change:\*\*/m);
  expect(body).not.toMatch(/^## (?:Mock-up|Estimate & dependencies)$/m);
});

test("the Checker's UI change message focuses the UI change picker button, which its label names", async () => {
  const problem = 'header ui_change must be "none", "minor" or "major", not "big"';
  const base = fleet();
  const fake = await mount("#/tickets/demo/new", async (url, init) =>
    url === `${DEMO}/tickets/check` ? Response.json({ problems: [problem], advisories: [], blocks: true }) : base(url, init));
  await type("Title", "Steer notes show on the run detail card");
  for (const entry of fake.scheduled.filter(each => each.ms === 600)) await act(async () => entry.fn());
  await flush();
  const button = picker("UI change");
  expect(button.getAttribute("aria-haspopup")).toBe("listbox");
  expect(button.getAttribute("aria-invalid")).toBe("true");
  const panel = screen.getByRole("region", { name: "Checker" });
  await act(async () => void fireEvent.click(within(panel).getByRole("button", { name: new RegExp(`UI change.*${problem}`) })));
  expect(document.activeElement).toBe(button);
});

test("with UI change open, the Checker's Summary message closes it and leaves focus on Summary", async () => {
  const base = fleet();
  const fake = await mount("#/tickets/demo/new", async (url, init) =>
    url === `${DEMO}/tickets/check` ? Response.json({ problems: ["'Summary' is empty"], advisories: [], blocks: true }) : base(url, init));
  await type("Title", "Steer notes show on the run detail card");
  for (const entry of fake.scheduled.filter(each => each.ms === 600)) await act(async () => entry.fn());
  await flush();
  await open("UI change");
  const jump = within(screen.getByRole("region", { name: "Checker" })).getByRole("button", { name: /Summary/ });
  await act(async () => {
    fireEvent.pointerDown(jump);
    fireEvent.pointerUp(jump);
    fireEvent.click(jump);
  });
  await act(settle);
  expect(screen.queryByRole("listbox", { name: "UI change" })).toBeNull();
  expect(document.activeElement === screen.getByRole("textbox", { name: "Summary" })).toBe(true);
});

test("with UI change open, clicking into Summary closes it and Summary takes the typing", async () => {
  await mount("#/tickets/demo/new");
  await open("UI change");
  const summary = screen.getByRole("textbox", { name: "Summary" });
  await userEvent.click(summary);
  await userEvent.keyboard("Notes show");
  await flush();
  expect(screen.queryByRole("listbox", { name: "UI change" })).toBeNull();
  expect((summary as HTMLTextAreaElement).value).toBe("Notes show");
});

test("with UI change open, pressing the Blast radius label closes it and opens Blast radius with focus in its list", async () => {
  await mount("#/tickets/demo/new");
  await open("UI change");
  const label = screen.getByText("Blast radius", { selector: "label" });
  await act(async () => {
    fireEvent.pointerDown(label);
    fireEvent.pointerUp(label);
    fireEvent.click(label);
  });
  await flush();
  expect(screen.queryByRole("listbox", { name: "UI change" })).toBeNull();
  expect(screen.getByRole("listbox", { name: "Blast radius" }).contains(document.activeElement)).toBe(true);
});

test("Blast radius offers Diff decides, Medium and High: Diff decides files no blast_radius and High files it after ui_change", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  expect(picker("Blast radius").textContent).toBe("Diff decides");
  expect(await options("Blast radius")).toEqual([["Diff decides", "true"], ["Medium", "false"], ["High", "false"]]);
  await press("File ticket");
  expect(posted(answer).body).not.toMatch(/^blast_radius/m);
  await choose("Blast radius", "High");
  await press("File ticket");
  expect(header(posted(answer, 1).body).slice(0, 4)).toEqual(["+++", 'ui_change = "none"', 'blast_radius = "high"', "estimate = 25"]);
});

const files = () => screen.queryAllByRole("textbox", { name: /^E\d+ File$/ }).map(input => (input as HTMLInputElement).value);

test("Evidence rows start numbered, renumber their files when moved, file in order and stop at six", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  expect(files()).toEqual([]);
  await press("+ Add a state");
  await press("+ Add a state");
  expect(files()).toEqual(["01-", "02-"]);
  await type("E1 File", "01-lane.png");
  await type("E1 State", "the lane");
  await type("E2 File", "02-row.png");
  await type("E2 State", "the row");
  await press("Move E2 up");
  expect(files()).toEqual(["01-row.png", "02-lane.png"]);
  await press("File ticket");
  expect(posted(answer).body.split("\n+++\n")[0]!.split("\n").filter(line => /^(?:\[\[evidence\]\]$|file = |state = )/.test(line))).toEqual([
    "[[evidence]]",
    'file = "01-row.png"',
    'state = "the row"',
    "[[evidence]]",
    'file = "02-lane.png"',
    'state = "the lane"',
  ]);
  for (let row = 3; row <= 6; row += 1) await press("+ Add a state");
  expect(files()).toEqual(["01-row.png", "02-lane.png", "03-", "04-", "05-", "06-"]);
  expect((screen.getByRole("button", { name: "+ Add a state" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("6 of 6")).toBeTruthy();
});

test("estimate stepping stops at 5 and 120 minutes and files those boundary values", async () => {
  const answer = rejectTicket();
  await mount("#/tickets/demo/new", fleet(answer.fetchImpl));
  await type("Title", "Steer notes show on the run detail card");
  for (let index = 0; index < 5; index += 1) await press("Decrease estimate");
  expect((screen.getByRole("spinbutton", { name: "Estimate" }) as HTMLInputElement).value).toBe("5");
  await press("File ticket");
  expect(posted(answer).body.match(/^estimate = .*$/gm)).toEqual(["estimate = 5"]);
  await act(async () => void fireEvent.change(screen.getByRole("spinbutton", { name: "Estimate" }), { target: { value: "115" } }));
  await press("Increase estimate");
  await press("Increase estimate");
  expect((screen.getByRole("spinbutton", { name: "Estimate" }) as HTMLInputElement).value).toBe("120");
  await press("File ticket");
  expect(posted(answer, 1).body.match(/^estimate = .*$/gm)).toEqual(["estimate = 120"]);
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
  expect(posted(answer).body.match(/^estimate = .*$/gm)).toEqual(["estimate = 45"]);
});
