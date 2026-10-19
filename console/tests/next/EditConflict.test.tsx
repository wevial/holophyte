import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, Status, TicketDetail } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { pickedBody, sectionParts } from "../../src/next/sectionPicks";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, SERVE } from "./needsFleet";

const PARTS_BASE = `# Steer notes show

## Summary

The notes show.

## Acceptance criteria

- [ ] Each note shows.

## Notes

First note.

## Notes

Second note.

## Evidence

- 01-notes.png

## Out of scope

- The fleet page
`;
const PARTS_THEIRS = `# Steer notes show

## Summary

Every note shows.

## Acceptance criteria

- [ ] Each note shows with its time.

## Notes

First note.

## Notes

Second note.

## Out of scope

- The fleet page
`;
const PARTS_MINE = `# Steer notes show under their run

## Summary

The notes show.

## Acceptance criteria

- [ ] Each note shows in bold.

## Notes

First note.

## Notes

Second note, edited.

## Evidence

- 01-notes.png

## Out of scope

- The fleet page
`;

test("sectionParts sorts each part by who changed it and pickedBody keeps theirs but for mine's parts", () => {
  const parts = sectionParts(PARTS_BASE, PARTS_THEIRS, PARTS_MINE);
  expect(parts.map(part => [part.key, part.side])).toEqual([
    ["Title", "mine"], ["Summary", "theirs"], ["Acceptance criteria", "both"], ["Notes", "same"],
    ["Notes#2", "mine"], ["Out of scope", "same"], ["Evidence", "theirs"],
  ]);
  expect(pickedBody(PARTS_THEIRS, PARTS_MINE, parts, { "Acceptance criteria": "theirs" })).toBe(`# Steer notes show under their run

## Summary

Every note shows.

## Acceptance criteria

- [ ] Each note shows with its time.

## Notes

First note.

## Notes

Second note, edited.

## Out of scope

- The fleet page
`);
});

test("a section mine kept against theirs' removal follows theirs' sections", () => {
  const base = "# Notes\n\n## Summary\n\nThe notes show.\n\n## Evidence\n\n- 01-notes.png\n\n## Notes\n\nOne.\n";
  const theirs = "# Notes\n\n## Summary\n\nThe notes show.\n\n## Notes\n\nOne.";
  const mine = "# Notes\n\n## Summary\n\nThe notes show.\n\n## Evidence\n\n- 02-notes.png\n\n## Notes\n\nOne.\n";
  const parts = sectionParts(base, theirs, mine);
  expect(parts.find(part => part.key === "Evidence")).toMatchObject({ side: "both", theirs: null });
  expect(pickedBody(theirs, mine, parts, { Evidence: "mine" })).toBe("# Notes\n\n## Summary\n\nThe notes show.\n\n## Notes\n\nOne.\n## Evidence\n\n- 02-notes.png\n\n");
  expect(pickedBody(theirs, mine, parts, { Evidence: "theirs" })).toBe(theirs);
});

test("a section headed Title is a part apart from the preamble", () => {
  const base = "# Notes\n\nIntro.\n\n## Title\n\nA\n";
  const theirs = "# Every note\n\nIntro.\n\n## Title\n\nA\n";
  const mine = "# Notes\n\nIntro.\n\n## Title\n\nB\n";
  const parts = sectionParts(base, theirs, mine);
  expect(parts.map(part => [part.key, part.side])).toEqual([["Title", "theirs"], ["Title#2", "mine"]]);
  expect(pickedBody(theirs, mine, parts, {})).toBe("# Every note\n\nIntro.\n\n## Title\n\nB\n");
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
const THEIR_SUMMARY = "The run page lists each steer note and when the agent read it.";
const MY_TITLE = "Steer notes show beside their run";

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
const REVISION_4 = demo2(4, BODY.replace(AC2, THEIR_AC2), "cli");
const REVISION_4_WIDER = demo2(4, BODY.replace(AC2, THEIR_AC2).replace("The run page lists each steer note the operator left.", THEIR_SUMMARY), "cli");
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

async function conflicted(save = () => new Response("unexpected", { status: 500 })) {
  await mount(save);
  await type("AC2", MY_AC2);
  await poll(REVISION_4);
}

async function widerConflict(save = () => new Response("unexpected", { status: 500 })) {
  await mount(save);
  await type("Title", MY_TITLE);
  await type("AC2", MY_AC2);
  await poll(REVISION_4_WIDER);
}

const criteriaPart = () => within(banner()!).getByRole("region", { name: "Acceptance criteria" });

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
beforeEach(() => storeToken(addressOf(DEMO), "tok-411"));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});


test("a newer revision polled under an edit shows both Acceptance criteria texts and holds Save", async () => {
  await conflicted();
  const alert = banner()!;
  expect(within(alert).getByText("DEMO-2 changed while you were editing.")).toBeTruthy();
  expect(alert.textContent).toMatch(/you started from revision 3 · cli saved revision 4 at \d\d:\d\d · it changed Acceptance criteria · nothing is saved yet/);
  const side = (label: string) => within(criteriaPart()).getByText(label).nextSibling?.textContent;
  expect([side("REVISION 4, THEIRS"), side("YOURS")]).toEqual([
    `- [ ] Each note shows on the run page.\n- [ ] ${THEIR_AC2}\n- [ ] A withdrawn note shows struck through.`,
    `- [ ] Each note shows on the run page.\n- [ ] ${MY_AC2}\n- [ ] A withdrawn note shows struck through.`,
  ]);
  const load = screen.getByRole("button", { name: "Load revision 4 with these picks" }) as HTMLButtonElement;
  expect(load.disabled).toBe(true);
  expect(within(alert).getByText("Pick a side for each section.")).toBeTruthy();
  expect(header()).toBe("revision 3 · newer revision 4 exists");
  expect(saveButton().disabled).toBe(true);
  expect(within(savePanel()).getByText("Resolve the newer revision first. Your edit is safe.")).toBeTruthy();
});

test("Keep mine loads revision 4 with my Title and criteria over their Summary and saves it as revision 5", async () => {
  await widerConflict(() => Response.json({ ticket: "DEMO-2", revision: 5 }));
  await act(async () => void fireEvent.click(within(criteriaPart()).getByRole("button", { name: "Keep mine" })));
  expect(within(criteriaPart()).getByRole("button", { name: "Keep mine" }).getAttribute("aria-pressed")).toBe("true");
  await press("Load revision 4 with these picks");
  expect(toast()).toBe("Revision 4 loaded with your picks. Review, then save as revision 5.");
  expect(banner()).toBeNull();
  expect(header()).toBe("revision 4 + your picks");
  expect(saveButton().textContent).toBe("Save revision 5");
  served = { ...REVISION_4_WIDER, current: { ...REVISION_4_WIDER.current!, revision: 5 } };
  await act(async () => void fireEvent.click(saveButton()));
  await flush();
  expect(puts).toEqual([{ ifMatch: "4", body: `# Steer notes show beside their run

## Summary

The run page lists each steer note and when the agent read it.

## Acceptance criteria

- [ ] Each note shows on the run page.
- [ ] A note's author shows beside it in bold.
- [ ] A withdrawn note shows struck through.

## Estimate & dependencies

Estimate: 30 min · Depends on: none
` }]);
});

test("Take theirs loads their criteria and Summary beside my Title", async () => {
  await widerConflict();
  await act(async () => void fireEvent.click(within(criteriaPart()).getByRole("button", { name: "Take theirs" })));
  await press("Load revision 4 with these picks");
  expect([box("Title").value, box("Summary").value, box("AC1").value, box("AC2").value, box("AC3").value])
    .toEqual([MY_TITLE, THEIR_SUMMARY, "Each note shows on the run page.", THEIR_AC2, "A withdrawn note shows struck through."]);
});

test("Take revision 4 drops my edit and sends nothing", async () => {
  await widerConflict();
  await press("Take revision 4, drop mine");
  expect(toast()).toBe("Took revision 4. Your edits were dropped.");
  expect(banner()).toBeNull();
  expect([box("Title").value, box("Summary").value, box("AC1").value, box("AC2").value, box("AC3").value])
    .toEqual(["Steer notes show on the run page", THEIR_SUMMARY, "Each note shows on the run page.", THEIR_AC2, "A withdrawn note shows struck through."]);
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
  expect(within(criteriaPart()).getByText("REVISION 4, THEIRS")).toBeTruthy();
  expect(header()).toBe("revision 3 · newer revision 4 exists");
  expect(saveButton().disabled).toBe(true);
});

test("a newer revision polled with no edit reloads the form without a banner", async () => {
  await mount(() => new Response("unexpected", { status: 500 }));
  await poll(REVISION_4_WIDER);
  expect(banner()).toBeNull();
  expect([box("Summary").value, box("AC2").value]).toEqual([THEIR_SUMMARY, THEIR_AC2]);
  expect(header()).toStartWith("revision 4 · ");
  expect(saveButton().textContent).toBe("Save revision 5");
});
