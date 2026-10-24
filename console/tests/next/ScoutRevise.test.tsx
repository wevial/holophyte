import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, ScoutAction, ScoutDetail, ScoutSubject, TicketDetail } from "../../src/lib/contract.gen";
import { ACTIONS_OFF } from "../../src/lib/actions";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;

const body = (title: string, summary: string) =>
  `# ${title}\n\n## Summary\n\n${summary}\n\n## Acceptance criteria\n\n- [ ] A writer waits at most 30 s.\n`;
const CURRENT = body("Retry a locked write", "A locked write is retried.");
const A2_TITLE = "Retry a locked write within the store's busy_timeout";
const A2_DRAFT = body(A2_TITLE, "A locked write waits on the store's busy_timeout, not a loop of its own.");

const item = (position: number, kind: ScoutAction["kind"], title: string, fields: Partial<ScoutAction> = {}): ScoutAction => ({
  position, pass: 1, title, why: `Why ${title}.`, kind, body: body(title, "A draft."), revises: null, ticket: null, filed_ms: null, ...fields,
});
const items: ScoutAction[] = [
  item(1, "fix", "Commit the listener's write before it calls publish"),
  item(2, "revise", A2_TITLE, { body: A2_DRAFT, revises: { ticket: "DEMO-7", revision: 2 } }),
];

const scout = (subject: ScoutSubject | null): ScoutDetail => ({
  ...served,
  scout: { ...served.scout, id: 31, phase: "done", subject, started_ms: NOW - 3_600_000, ended_ms: NOW - 1_800_000 },
  actions: items,
});

const ticket = (revision: number, current: string): TicketDetail => ({
  ...servedTicket,
  ticket: "DEMO-7",
  title: "Retry a locked write",
  status: "backlog",
  run: null,
  runs: [],
  steers: [],
  body: current,
  current: { ...servedTicket.current!, revision, body: current, priority: 2, column: "backlog" },
});

const board = (revision: number): Board => ({
  ...pinned,
  editable: true,
  stories: [],
  columns: [{ state: "backlog", tickets: [{ ...template, ticket: "DEMO-7", title: "Retry a locked write", column: "backlog", revision, run: null } as BoardTicket] }],
});

interface Daemon {
  subject?: ScoutSubject | null;
  revision?: number;
  actions?: boolean;
  current?: string;
}

/** DEMO answering scout 31 and DEMO-7 at `daemon.revision`, a save of DEMO-7 with the next revision; every request is kept. */
function demo({ subject = { kind: "defect", ref: "812" }, revision = 2, actions = true, current = CURRENT }: Daemon = {}) {
  const seen: { url: string; method: string; ifMatch: string | null; body: unknown }[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fetch: Fetch = async (url, init) => {
    const method = init?.method ?? "GET";
    seen.push({ url, method, ifMatch: new Headers(init?.headers).get("if-match"), body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    if (url === `${DEMO}/scouts/31`) return Response.json(scout(subject));
    if (url === `${DEMO}/tickets/DEMO-7` && method === "PUT") return Response.json({ ticket: "DEMO-7", revision: revision + 1 });
    if (url === `${DEMO}/tickets/DEMO-7`) return Response.json(ticket(revision, current));
    if (url === `${DEMO}/board`) return Response.json(board(revision));
    if (url === `${DEMO}/tickets/check`) return Response.json({ problems: [], advisories: [], blocks: false });
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions });
  };
  return { fetch, seen };
}

const settled = async () => {
  for (let round = 0; round < 6; round += 1) await act(settle);
};

async function mount(fetch: Fetch) {
  window.location.hash = "#/scouts/demo/31";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  await settled();
}

const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  await settled();
};

const facts = () => document.querySelector<HTMLElement>("[data-scout-facts]")!;
const region = () => screen.getByRole("region", { name: "Action items" });
const rowOf = (id: string) =>
  Array.from(within(region()).getByRole("list", { name: "Action items" }).children).find((row) => row.querySelector("[data-action-id]")!.textContent === id) as HTMLElement;
const reviseButton = () => within(rowOf("A2")).getByRole<HTMLButtonElement>("button", { name: "Revise" });
const title = () => (screen.getByRole("textbox", { name: "Title" }) as HTMLInputElement).value;
const toast = () => document.querySelector("[data-toast]")?.textContent;
const filePosts = (seen: { url: string; method: string }[]) => seen.filter((each) => each.url === `${DEMO}/actions/scout-file` && each.method === "POST");

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => {
  localStorage.clear();
  storeToken(addressOf(DEMO), "tok-538");
});
afterEach(() => {
  cleanup();
  window.history.replaceState(null, "");
  window.location.hash = "";
});

test("the facts line links a run, defect, ticket or pull request subject and reads no about without one", async () => {
  const cases: [ScoutSubject, string, string][] = [
    [{ kind: "defect", ref: "812" }, "the defect in run 812", "#/runs/demo/812"],
    [{ kind: "run", ref: "812" }, "run 812", "#/runs/demo/812"],
    [{ kind: "ticket", ref: "DEMO-7" }, "DEMO-7", "#/tickets/demo/DEMO-7"],
    [{ kind: "pr", ref: "https://github.com/acme/demo/pull/7" }, "PR #7", "https://github.com/acme/demo/pull/7"],
  ];
  for (const [subject, words, href] of cases) {
    await mount(demo({ subject }).fetch);
    expect(facts().textContent).toContain(`· about ${words}`);
    const link = within(facts()).getByRole<HTMLAnchorElement>("link", { name: `about ${words}` });
    expect(link.getAttribute("href")!.endsWith(href)).toBe(true);
    cleanup();
  }
  await mount(demo({ subject: null }).fetch);
  expect(facts().textContent).toStartWith("scout 31");
  expect(facts().textContent).not.toContain("about");
});

test("a revise item reads its tag and Revise in place of File and is not counted to file", async () => {
  await mount(demo().fetch);
  expect(within(region()).getByRole("heading").textContent).toBe("Action items1 to file");
  expect(within(rowOf("A1")).getByRole("button").textContent).toBe("File");
  expect(rowOf("A2").querySelector("[data-action-kind]")!.textContent).toBe("revise");
  expect(reviseButton().disabled).toBe(false);
  expect(within(rowOf("A2")).queryByRole("button", { name: "File" })).toBeNull();
});

test("Revise opens DEMO-7's edit page with the draft over revision 2, the changed fields marked, and files nothing", async () => {
  const { fetch, seen } = demo();
  await mount(fetch);
  await click(reviseButton());
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-7/edit");
  expect(title()).toBe(A2_TITLE);
  expect(screen.getByText("revision 2 + scout 31's draft")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Title" }).closest("[data-changed]")).not.toBeNull();
  expect(filePosts(seen)).toHaveLength(0);
});

test("Save on the handed draft posts it at revision 2, toasts revision 3 and opens DEMO-7", async () => {
  const { fetch, seen } = demo();
  await mount(fetch);
  await click(reviseButton());
  await click(screen.getByRole("button", { name: "Save revision 3" }));
  const edits = seen.filter((each) => each.url === `${DEMO}/tickets/DEMO-7` && each.method === "PUT");
  expect(edits.map((each) => [each.ifMatch, (each.body as { body: string }).body])).toEqual([["2", A2_DRAFT]]);
  expect(toast()).toBe("Saved DEMO-7 as revision 3.");
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-7");
});

test("a draft that drops a stored section shows it removed and counts it among the changes", async () => {
  await mount(demo({ current: `${CURRENT}\n## Open questions\n\nShould a writer back off?\n` }).fetch);
  await click(reviseButton());
  const removed = screen.getByText("Should a writer back off?").closest<HTMLElement>("[data-changed]")!;
  expect(removed.textContent).toContain("Open questions");
  expect(removed.textContent).toContain("removed on save");
  expect(screen.getByText(/^3 changes · Title, Summary, Open questions removed ·/)).toBeTruthy();
});

test("a reload of the edit page drops the handed draft and shows DEMO-7 as stored", async () => {
  const { fetch } = demo();
  await mount(fetch);
  await click(reviseButton());
  cleanup();
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fakeDeps(fetch).deps} />
    </QueryClientProvider>,
  );
  await settled();
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-7/edit");
  expect(title()).toBe("Retry a locked write");
  expect(screen.queryByText(/scout 31's draft/)).toBeNull();
});

test("a revise item's title opens the edit page with the draft as Revise does", async () => {
  await mount(demo().fetch);
  await click(within(rowOf("A2")).getByRole("link", { name: A2_TITLE }));
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-7/edit");
  expect(title()).toBe(A2_TITLE);
  expect(screen.getByText("revision 2 + scout 31's draft")).toBeTruthy();
});

test("a draft of revision 2 opened over revision 3 names the revision it was drafted against", async () => {
  await mount(demo({ revision: 3 }).fetch);
  await click(reviseButton());
  expect(screen.getByText("revision 3 + scout 31's draft of revision 2")).toBeTruthy();
});

test("actions off disables Revise with the actions-off title", async () => {
  await mount(demo({ actions: false }).fetch);
  expect([reviseButton().disabled, reviseButton().title]).toEqual([true, ACTIONS_OFF]);
});
