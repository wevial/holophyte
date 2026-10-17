import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, Status, TicketDetail } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import type { EditFields } from "../../src/next/ticketEdit";
import { mergeEdit } from "../../src/next/ticketForm";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, SERVE } from "./needsFleet";

const BASE: EditFields = {
  title: "Steer notes show on the run page",
  summary: "The run page lists each steer note.",
  what: "List the notes.", why: "", how: "Read the steers.",
  inScope: "The run page", outOfScope: "", verify: "bun --cwd=console test", notes: "",
  mockupLink: "", mockupApproval: "", mockupImages: "",
  criteria: ["Each note shows.", "A note's author shows beside it.", "A withdrawn note shows struck through."],
  criteriaFrom: [0, 1, 2],
  criteriaDetail: ["", "", ""],
  estimate: "30", priority: 2, dependsOn: [], uiChange: null,
  others: [{ index: 6, name: "Evidence", text: "- 01-notes.png" }],
};

test("mergeEdit sorts each field by who changed it and puts mine on top of theirs", () => {
  const theirs: EditFields = { ...BASE, summary: "Each steer note shows on the run page.", criteria: [BASE.criteria[0]!, "A note's author and time show.", BASE.criteria[2]!] };
  const mine: EditFields = { ...BASE, title: "Steer notes show under their run", criteria: [BASE.criteria[0]!, "A note's author shows in bold.", BASE.criteria[2]!] };
  const merge = mergeEdit(BASE, theirs, mine);
  expect(merge.onlyTheirs).toEqual(["Summary"]);
  expect(merge.onlyMine).toEqual(["Title"]);
  expect(merge.both).toEqual([{ name: "AC2", theirs: "A note's author and time show.", mine: "A note's author shows in bold." }]);
  expect(merge.onTop).toEqual({
    title: "Steer notes show under their run",
    summary: "Each steer note shows on the run page.",
    what: "List the notes.", why: "", how: "Read the steers.",
    inScope: "The run page", outOfScope: "", verify: "bun --cwd=console test", notes: "",
    mockupLink: "", mockupApproval: "", mockupImages: "",
    criteria: ["Each note shows.", "A note's author shows in bold.", "A withdrawn note shows struck through."],
    criteriaFrom: [0, 1, 2],
    criteriaDetail: ["", "", ""],
    estimate: "30", priority: 2, dependsOn: [], uiChange: null,
    others: [{ index: 6, name: "Evidence", text: "- 01-notes.png" }],
  });
});

const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const template = pinned.columns.flatMap((column) => column.tickets)[0]!;
const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;

const AC2 = "A note's author shows beside it.";
const THEIR_AC2 = "A note's author and its time show beside it.";
const MY_AC2 = "A note's author shows beside it in bold.";
const BODY = `# Steer notes show on the run page

## Summary

The run page lists each steer note the operator left.

## Acceptance criteria

- [ ] Each note shows on the run page.
- [ ] ${AC2}
- [ ] A withdrawn note shows struck through.

## Estimate & dependencies

Estimate: 30 min · Depends on: none
`;
const THEIR_AC3 = "A withdrawn note shows struck through with who withdrew it.";
const THEIR_BODY = BODY.replace(AC2, THEIR_AC2).replace("A withdrawn note shows struck through.", THEIR_AC3);

const demo2 = (revision: number, body: string, author: string): TicketDetail => ({
  ...servedTicket,
  ticket: "DEMO-2",
  title: "Steer notes show on the run page",
  status: "ready",
  run: null,
  runs: [],
  steers: [],
  body,
  current: { ...servedTicket.current!, revision, at: NOW - 30 * MINUTE, author, body, priority: 2, column: "ready" },
});
const REVISION_3 = demo2(3, BODY, "console");
const REVISION_4 = demo2(4, THEIR_BODY, "cli");
const board: Board = {
  ...pinned,
  editable: true,
  stories: [],
  columns: [{ state: "ready", tickets: [{ ...template, ticket: "DEMO-2", title: "Steer notes show on the run page", column: "ready", revision: 3, run: null } as BoardTicket]}],
};

let served: TicketDetail;
let reads = 0;
let puts: { ifMatch: string | null; body: string }[] = [];
let queries: ReturnType<typeof consoleQueryClient>;

async function flush() {
  for (let round = 0; round < 6; round += 1) await act(settle);
}

/** DEMO serving `served` as DEMO-2 behind an editable board; each
 *  `PUT /tickets/DEMO-2` is answered by `save`. */
async function mount(save: () => Response) {
  window.location.hash = "#/tickets/demo/DEMO-2/edit";
  const polls = fleet({ items: [] }, { items: [] });
  served = REVISION_3;
  reads = 0;
  puts = [];
  const fake = fakeDeps(async (url, init) => {
    if (url === `${DEMO}/tickets/check`) return Response.json({ problems: [], advisories: [], blocks: false });
    if (init?.method === "PUT") {
      puts.push({ ifMatch: new Headers(init.headers).get("if-match"), body: (JSON.parse(String(init.body)) as { body: string }).body });
      return save();
    }
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions: true });
    if (url === `${DEMO}/board`) return Response.json(board);
    if (url === `${DEMO}/tickets/DEMO-2`) {
      reads += 1;
      return Response.json(served);
    }
    if (url.endsWith("/board")) return new Response("not found", { status: 404 });
    return polls(url);
  });
  queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={{ ...fake.deps, now: () => NOW, timer: () => () => {} }} />
    </QueryClientProvider>,
  );
  await flush();
}

async function poll(ticket: TicketDetail) {
  served = ticket;
  await act(async () => void (await queries.refetchQueries()));
  await flush();
}

const box = (name: string) => screen.getByRole("textbox", { name }) as HTMLInputElement;
const saveButton = () => screen.getByRole("button", { name: /^Save revision|^Saving/ }) as HTMLButtonElement;
const savePanel = () => screen.getByRole("complementary", { name: "Save revision" });
const banner = () => screen.queryByRole("alert", { name: "Newer revision" });
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const press = async (name: string) => {
  await act(async () => void fireEvent.click(screen.getByRole("button", { name })));
  await flush();
};
const type = async (name: string, value: string) => act(async () => void fireEvent.change(box(name), { target: { value } }));
const header = () => document.querySelector("#page-title + p")?.textContent;

async function conflicted() {
  await mount(() => new Response("unexpected", { status: 500 }));
  await type("AC2", MY_AC2);
  await poll(REVISION_4);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
beforeEach(() => storeToken(addressOf(DEMO), "tok-411"));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a newer revision polled under an edit shows both AC2 texts and holds Save", async () => {
  await conflicted();
  const alert = banner()!;
  expect(within(alert).getByText("DEMO-2 changed while you were editing.")).toBeTruthy();
  expect(alert.textContent).toMatch(/you started from revision 3 · cli saved revision 4 at \d\d:\d\d · it changed Acceptance criteria only · nothing is saved yet/);
  const clash = alert.querySelector("[data-clash=AC2]")!;
  expect([...clash.children].map(side => side.textContent)).toEqual([`AC2 · REVISION 4, THEIRS${THEIR_AC2}`, `AC2 · YOURS${MY_AC2}`]);
  expect(header()).toBe("revision 3 · newer revision 4 exists");
  expect(saveButton().disabled).toBe(true);
  expect(within(savePanel()).getByText("Resolve the newer revision first. Your edit is safe.")).toBeTruthy();
});

test("Put mine on top loads revision 4 with my AC2 and saves it as revision 5", async () => {
  await conflicted();
  await press("Put mine on top of revision 4");
  expect(toast()).toBe("Revision 4 loaded with your edits on top. Review, then save as revision 5.");
  expect(banner()).toBeNull();
  expect(header()).toBe("revision 4 + your edit");
  expect([box("AC2").value, box("AC3").value]).toEqual([MY_AC2, THEIR_AC3]);
  expect(saveButton().textContent).toBe("Save revision 5");
  served = { ...REVISION_4, current: { ...REVISION_4.current!, revision: 5 } };
  await act(async () => void fireEvent.click(saveButton()));
  await flush();
  expect(puts).toEqual([{ ifMatch: "4", body: THEIR_BODY.replace(THEIR_AC2, MY_AC2) }]);
});

test("Take revision 4 drops my edit and sends nothing", async () => {
  await conflicted();
  await press("Take revision 4, drop mine");
  expect(toast()).toBe("Took revision 4. Your edits were dropped.");
  expect(banner()).toBeNull();
  expect([box("Title").value, box("Summary").value, box("AC1").value, box("AC2").value, box("AC3").value])
    .toEqual(["Steer notes show on the run page", "The run page lists each steer note the operator left.", "Each note shows on the run page.", THEIR_AC2, THEIR_AC3]);
  expect(saveButton().disabled).toBe(true);
  expect(within(savePanel()).getByText("No changes yet.")).toBeTruthy();
  expect(puts).toEqual([]);
});

test("a Save answered 409 reads DEMO-2 again and opens the same banner", async () => {
  await mount(() => Response.json({ error: "revision mismatch", current: 4 }, { status: 409 }));
  await type("AC2", MY_AC2);
  served = REVISION_4;
  const before = reads;
  await act(async () => void fireEvent.click(saveButton()));
  await flush();
  expect(puts.map(put => put.ifMatch)).toEqual(["3"]);
  expect(reads).toBeGreaterThan(before);
  expect(within(banner()!).getByText("DEMO-2 changed while you were editing.")).toBeTruthy();
  expect(header()).toBe("revision 3 · newer revision 4 exists");
  expect(saveButton().disabled).toBe(true);
});

test("a newer revision polled with no edit reloads the form without a banner", async () => {
  await mount(() => new Response("unexpected", { status: 500 }));
  await poll(REVISION_4);
  expect(banner()).toBeNull();
  expect([box("AC2").value, box("AC3").value]).toEqual([THEIR_AC2, THEIR_AC3]);
  expect(header()).toStartWith("revision 4 · ");
  expect(saveButton().textContent).toBe("Save revision 5");
});
