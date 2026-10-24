import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, ScoutAction, ScoutDetail } from "../../src/lib/contract.gen";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const board = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;

const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const NOON = at(12, 0);
const CHECK_DELAY_MS = 600;

const draft = (title: string, depends = "") =>
  `+++\nui_change = "none"\nestimate = 25\ndepends_on = [${depends}]\n+++\n# ${title}\n\n## Summary\n\nOne writer for the store.\n`;
const A3_TITLE = "Raise the loop's busy_timeout to 30 s and log each wait over 5 s";
const A4_TITLE = "Route the digest's writes through the store writer";
const REFUSAL = "'Depends on' names no ticket of this project: DEMO-9";

const item = (position: number, kind: ScoutAction["kind"], title: string, why: string, fields: Partial<ScoutAction> = {}): ScoutAction => ({
  position, pass: 1, title, why, kind, body: draft(title), ticket: null, filed_ms: null, ...fields,
});

const items: ScoutAction[] = [
  item(1, "fix", "Commit the listener's write before it calls publish", "F1: the open transaction blocks every loop claim."),
  item(2, "test", "A listener publish never blocks a loop claim", "Keeps the wave-1 reproduction as a regression test.", { ticket: "DEMO-213", filed_ms: at(11, 38) }),
  item(3, "fix", A3_TITLE, "F2: 35 of 37 locks came within 6 s of a publish."),
  item(4, "decide", A4_TITLE, "F3 is likely, not shown.", { body: draft(A4_TITLE, '"DEMO-9"') }),
];

const done = (actions: ScoutAction[] = items): ScoutDetail => ({
  ...served,
  scout: { ...served.scout, id: 31, phase: "done", started_ms: at(11, 0), ended_ms: at(11, 33) },
  actions,
});

interface Daemon {
  scout: ScoutDetail;
  actions?: boolean;
  file?: () => Response | Promise<Response>;
  check?: () => Response;
  filing?: () => Response;
  other?: ScoutDetail;
}

/** DEMO answering `/scouts/31` from `daemon.scout` at each read, its file route from `daemon.file`; every request is kept. */
function scoutFetch(daemon: Daemon) {
  const seen: { url: string; method: string; body: unknown }[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fetch: Fetch = async (url, init) => {
    seen.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    if (url === `${DEMO}/scouts/31`) return Response.json(daemon.scout);
    if (url === `${DEMO}/scouts/32` && daemon.other) return Response.json(daemon.other);
    if (url === `${DEMO}/tickets` && daemon.filing) return daemon.filing();
    if (url === `${DEMO}/actions/scout-file` && daemon.file) return daemon.file();
    if (url === `${DEMO}/board`) return Response.json({ ...board, editable: true });
    if (url === `${DEMO}/tickets/check` && daemon.check) return daemon.check();
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: daemon.actions ?? true });
  };
  return { fetch, seen };
}

const settled = async () => {
  for (let round = 0; round < 6; round += 1) await act(settle);
};

async function mount(fetch: Fetch) {
  window.location.hash = "#/scouts/demo/31";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOON;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  await settled();
  return fake;
}

const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  await settled();
};

const region = () => screen.getByRole("region", { name: "Action items" });
const heading = () => within(region()).getByRole("heading").textContent;
const rows = () => Array.from(within(region()).getByRole("list", { name: "Action items" }).children) as HTMLElement[];
const rowOf = (id: string) => rows().find((row) => row.querySelector("[data-action-id]")!.textContent === id)!;
const fileOf = (id: string) => within(rowOf(id)).getByRole("button") as HTMLButtonElement;
const toast = () => document.querySelector("[data-toast]")?.textContent;
const reads = (seen: { url: string }[]) => seen.filter((each) => each.url === `${DEMO}/scouts/31`).length;
const posts = (seen: { url: string; method: string; body: unknown }[]) => seen.filter((each) => each.url === `${DEMO}/actions/scout-file` && each.method === "POST");
const refusal = (detail: string) => () => Response.json({ ok: false, detail });

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  process.env.TZ = zone;
});
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a done scout's Action items count the unfiled, list A1 to A4 in order and stamp the filed one", async () => {
  await mount(scoutFetch({ scout: done([...items].reverse()) }).fetch);
  expect(heading()).toBe("Action items3 to file");
  expect(rows().map((row) => [
    row.querySelector("[data-action-id]")!.textContent,
    row.querySelector("[data-action-kind]")!.textContent,
    row.querySelector("[data-action-title]")!.textContent,
    row.querySelector("[data-action-why]")!.textContent,
  ])).toEqual(items.map((each) => [`A${each.position}`, each.kind, each.title, each.why]));
  const filed = rowOf("A2").querySelector("[data-filed]")!;
  expect(filed.textContent).toBe("Filed DEMO-213 · Backlog · 11:38");
  expect(within(rowOf("A2")).queryByRole("button")).toBeNull();
  expect(within(filed as HTMLElement).getByRole("link", { name: "DEMO-213" }).getAttribute("href")).toEndWith("#/tickets/demo/DEMO-213");
  for (const id of ["A1", "A3", "A4"]) expect(fileOf(id).textContent).toBe("File");
});

test("File posts the scout and item, reads Filing… while out, toasts the key and reads the scout again", async () => {
  let answer: (response: Response) => void = () => {};
  const daemon: Daemon = { scout: done(), file: () => new Promise((resolve) => (answer = resolve)) };
  const { fetch, seen } = scoutFetch(daemon);
  await mount(fetch);
  const before = reads(seen);
  await click(fileOf("A1"));
  expect([fileOf("A1").textContent, fileOf("A1").disabled]).toEqual(["Filing…", true]);
  expect(posts(seen).map((each) => each.body)).toEqual([{ scout: 31, item: 1 }]);

  daemon.scout = done(items.map((each) => (each.position === 1 ? { ...each, ticket: "DEMO-214", filed_ms: at(11, 59) } : each)));
  answer(Response.json({ ok: true, ticket: "DEMO-214", problems: [], advisories: [], detail: "filed" }));
  await settled();
  expect(posts(seen)).toHaveLength(1);
  expect(toast()).toBe("Filed DEMO-214 in demo Backlog.");
  expect(reads(seen)).toBeGreaterThan(before);
  expect(rowOf("A1").querySelector("[data-filed]")!.textContent).toStartWith("Filed DEMO-214");
  expect(heading()).toBe("Action items2 to file");
});

test("a checker refusal toasts its first problem and opens New ticket with the item's draft, the stored draft untouched", async () => {
  const stored = JSON.stringify({ body: draft("A draft kept earlier"), priority: 2, savedAt: at(10, 0) });
  localStorage.setItem("holo.newTicket.demo", stored);
  const daemon: Daemon = {
    scout: done(),
    file: () => Response.json({ ok: false, ticket: null, problems: [REFUSAL], advisories: [], detail: REFUSAL }),
    check: () => Response.json({ problems: [REFUSAL], advisories: [], blocks: true }),
  };
  const { fetch, seen } = scoutFetch(daemon);
  const fake = await mount(fetch);
  await click(fileOf("A4"));
  expect(toast()).toBe(`The checker refused A4: ${REFUSAL}`);
  expect(window.location.hash).toBe("#/tickets/demo/new");
  expect((screen.getByRole("textbox", { name: "Title" }) as HTMLInputElement).value).toBe(A4_TITLE);
  expect(screen.queryByText(/restored\./)).toBeNull();

  const check = fake.scheduled.find((entry) => entry.ms === CHECK_DELAY_MS)!;
  await act(async () => check.fn());
  await settled();
  const checked = seen.filter((each) => each.url === `${DEMO}/tickets/check`);
  expect((checked.at(-1)!.body as { body: string }).body).toContain(`# ${A4_TITLE}\n`);
  const panel = screen.getByRole("region", { name: "Checker" });
  expect(within(panel).getByRole("list", { name: "Checker messages" }).textContent).toContain(REFUSAL);
  expect(localStorage.getItem("holo.newTicket.demo")).toBe(stored);
});

test("an item's title opens New ticket with its draft and files nothing", async () => {
  const { fetch, seen } = scoutFetch({ scout: done() });
  await mount(fetch);
  await click(within(rowOf("A3")).getByRole("link", { name: A3_TITLE }));
  expect(window.location.hash).toBe("#/tickets/demo/new");
  expect((screen.getByRole("textbox", { name: "Title" }) as HTMLInputElement).value).toBe(A3_TITLE);
  expect(posts(seen)).toHaveLength(0);
});

test("filing a handed draft from New ticket leaves the stored draft for the project as it was", async () => {
  const stored = JSON.stringify({ body: draft("A draft kept earlier"), priority: 2, savedAt: at(10, 0) });
  localStorage.setItem("holo.newTicket.demo", stored);
  const daemon: Daemon = {
    scout: done(),
    check: () => Response.json({ problems: [], advisories: [], blocks: false }),
    filing: () => Response.json({ ticket: "DEMO-215", revision: 1 }, { status: 201 }),
  };
  const fake = await mount(scoutFetch(daemon).fetch);
  await click(within(rowOf("A3")).getByRole("link", { name: A3_TITLE }));
  const check = fake.scheduled.find((entry) => entry.ms === CHECK_DELAY_MS)!;
  await act(async () => check.fn());
  await settled();
  await click(screen.getByRole("button", { name: "File ticket" }));
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-215");
  expect(localStorage.getItem("holo.newTicket.demo")).toBe(stored);
});

const visit = async (hash: string) => {
  await act(async () => {
    window.location.hash = hash;
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await settled();
};

test("a refusal under scout 31's A1 does not read under a cached scout 32's A1", async () => {
  const other = { ...done(), scout: { ...done().scout, id: 32 } };
  await mount(scoutFetch({ scout: done(), other, file: refusal("demo has no [board]") }).fetch);
  await visit("#/scouts/demo/32");
  await visit("#/scouts/demo/31");
  await click(fileOf("A1"));
  expect(within(rowOf("A1")).getByRole("alert").textContent).toBe("demo has no [board]");
  await visit("#/scouts/demo/32");
  expect(document.querySelector("[data-scout-facts]")!.textContent).toStartWith("scout 32");
  expect(within(rowOf("A1")).queryByRole("alert")).toBeNull();
});

test("an already-filed answer reads under the row and reads the scout again on the Scout page", async () => {
  const detail = "scout 31 item 1 is already filed as DEMO-214";
  const { fetch, seen } = scoutFetch({ scout: done(), file: () => Response.json({ ok: false, ticket: "DEMO-214", problems: [], advisories: [], detail }) });
  await mount(fetch);
  const before = reads(seen);
  await click(fileOf("A1"));
  expect(within(rowOf("A1")).getByRole("alert").textContent).toBe(detail);
  expect(window.location.hash).toBe("#/scouts/demo/31");
  expect(reads(seen)).toBeGreaterThan(before);
});

test("a 404 or a board refusal reads under the row in the bad tone and File is enabled again", async () => {
  const answers: [() => Response, string][] = [
    [() => Response.json({ error: "no such action item" }, { status: 404 }), `${DEMO}/actions/scout-file answered 404: no such action item`],
    [refusal("demo has no [board]"), "demo has no [board]"],
  ];
  for (const [file, said] of answers) {
    await mount(scoutFetch({ scout: done(), file }).fetch);
    await click(fileOf("A1"));
    const alert = within(rowOf("A1")).getByRole("alert");
    expect([alert.textContent, alert.classList.contains("text-bad")]).toEqual([said, true]);
    expect(window.location.hash).toBe("#/scouts/demo/31");
    expect([fileOf("A1").textContent, fileOf("A1").disabled]).toEqual(["File", false]);
    cleanup();
  }
});

test("actions off disables each File with the actions-off title", async () => {
  await mount(scoutFetch({ scout: done(), actions: false }).fetch);
  for (const id of ["A1", "A3", "A4"]) expect([fileOf(id).disabled, fileOf(id).title]).toEqual([true, ACTIONS_OFF]);
});

test("a swarming scout with no actions has no Action items region", async () => {
  const swarming = { ...done([]), scout: { ...done().scout, phase: "swarming" as const, ended_ms: null } };
  await mount(scoutFetch({ scout: swarming }).fetch);
  expect(screen.getByRole("region", { name: "Progress" })).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Action items" })).toBeNull();
});

test("an action of pass 2 reads pass 2 and a pass-1 action reads no pass", async () => {
  await mount(scoutFetch({ scout: done(items.map((each) => (each.position === 3 ? { ...each, pass: 2 } : each))) }).fetch);
  expect(rowOf("A3").querySelector("[data-action-pass]")?.textContent).toBe("pass 2");
  expect(rowOf("A1").querySelector("[data-action-pass]")).toBeNull();
});
