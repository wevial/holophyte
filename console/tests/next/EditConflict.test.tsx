import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, BoardTicket, Status, TicketDetail } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { changedNames, composeEdit, loadEdit, type EditFields } from "../../src/next/ticketEdit";
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

test("a section I edited that their revision removed is a clash and is added back on top", () => {
  const base = "# Notes show\n\n## Summary\n\nThe notes show.\n\n## Evidence\n\n- 01-notes.png\n";
  const theirs = "# Notes show\n\n## Summary\n\nEvery note shows.\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map(other => other.name === "Evidence" ? { ...other, text: "- 02-notes.png" } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(merge.both).toEqual([{ name: "Evidence", theirs: "(removed)", mine: "- 02-notes.png" }]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe("# Notes show\n\n## Summary\n\nEvery note shows.\n\n## Evidence\n\n- 02-notes.png\n");
});

test("an edit to the second of two sections sharing a heading stays on that section on top", () => {
  const base = "# Notes show\n\n## Evidence\n\n- 01-notes.png\n\n## Evidence\n\n- 02-notes.png\n";
  const theirs = base.replace("# Notes show", "# Every note shows");
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map((other, at) => at === 1 ? { ...other, text: "- 03-notes.png" } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect([merge.onlyTheirs, merge.onlyMine, merge.both]).toEqual([["Title"], ["Evidence"], []]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(theirs.replace("- 02-notes.png", "- 03-notes.png"));
});

test("an edit to a section their revision now draws as fields replaces that section rather than repeating it", () => {
  const base = "# Notes show\n\n## Summary\n\n<!-- one line -->\nThe notes show.\n\n## Evidence\n\n- 01-notes.png\n";
  const theirs = base.replace("<!-- one line -->\nThe notes show.", "Every note shows.");
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map(other => other.name === "Summary" ? { ...other, text: "<!-- one line -->\nEach note shows." } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(merge.both).toEqual([{ name: "Summary", theirs: "Every note shows.", mine: "<!-- one line -->\nEach note shows." }]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(base.replace("The notes show.", "Each note shows."));
});

test("a section their revision only redraws as fields shows their commands, not a removal", () => {
  const base = "# Notes show\n\n## Verify command(s)\n\n<!-- each on its own line -->\n```\nruff check .\n```\n";
  const theirs = "# Notes show\n\n## Verify command(s)\n\n```\nruff check .\n```\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: [{ ...read.others[0]!, text: "<!-- each on its own line -->\n```\nbun --cwd=console test\n```" }] };
  const merge = mergeEdit(read, loadEdit(theirs, null), mine);
  expect(merge.both).toEqual([{ name: "Verify command(s)", theirs: "ruff check .", mine: "<!-- each on its own line -->\n```\nbun --cwd=console test\n```" }]);
});

test("an edit to the second of two repeated sections their revision removed shows it removed and adds it back after the first", () => {
  const base = "# Notes show\n\n## Verify command(s)\n\n```\nruff check .\n```\n\n## Verify command(s)\n\n```\nbun --cwd=console test\n```\n";
  const theirs = "# Notes show\n\n## Verify command(s)\n\n```\nruff check .\n```\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: [{ ...read.others[0]!, text: "```\nbun --cwd=console run build\n```" }] };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(merge.both).toEqual([{ name: "Verify command(s)", theirs: "(removed)", mine: "```\nbun --cwd=console run build\n```" }]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(base.replace("bun --cwd=console test", "bun --cwd=console run build"));
});

test("criteria their revision only redraws as fields show each criterion's detail under it", () => {
  const base = "# Notes show\n\n## Acceptance criteria\n\n<!-- one per line -->\n- [ ] Each note shows.\n  Under the run.\n";
  const theirs = "# Notes show\n\n## Acceptance criteria\n\n- [ ] Each note shows.\n  Under its run.\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: [{ ...read.others[0]!, text: "<!-- one per line -->\n- [ ] Every note shows.\n  Under the run." }] };
  const merge = mergeEdit(read, loadEdit(theirs, null), mine);
  expect(merge.both).toEqual([{ name: "Acceptance criteria", theirs: "Each note shows.\n  Under its run.", mine: "<!-- one per line -->\n- [ ] Every note shows.\n  Under the run." }]);
});

test("a section restored by one rebase survives the next", () => {
  const base = "# Notes show\n\n## Summary\n\nThe notes show.\n\n## Evidence\n\n- 01-notes.png\n";
  const fourth = "# Notes show\n\n## Summary\n\nThe notes show.\n";
  const fifth = fourth.replace("# Notes show", "# Every note shows");
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: [{ ...read.others[0]!, text: "- 02-notes.png" }] };
  const fourthFields = loadEdit(fourth, null);
  const onFourth = composeEdit(fourth, fourthFields, mergeEdit(read, fourthFields, mine).onTop);
  const fifthFields = loadEdit(fifth, null);
  const merge = mergeEdit(fourthFields, fifthFields, loadEdit(onFourth, null));
  expect([merge.onlyTheirs, merge.onlyMine, merge.both]).toEqual([["Title"], ["Evidence"], []]);
  expect(composeEdit(fifth, fifthFields, merge.onTop)).toBe(`${fifth}\n## Evidence\n\n- 02-notes.png\n`);
});

test("an edit to the second of two sections sharing a heading stays on it when their revision makes the first one plain text", () => {
  const base = "# Notes show\n\n## Verify command(s)\n\n```\nruff check .\n```\n\n## Verify command(s)\n\n```\nbun --cwd=console test\n```\n";
  const theirs = base.replace("## Verify command(s)\n\n```\nruff", "## Verify command(s)\n\n<!-- each on its own line -->\n```\nruff");
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: [{ ...read.others[0]!, text: "```\nbun --cwd=console run build\n```" }] };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(merge.both).toEqual([]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(theirs.replace("bun --cwd=console test", "bun --cwd=console run build"));
});

test("my unlabelled What / Why / How text survives on top of a revision that removed the section", () => {
  const base = "# Notes show\n\n## Summary\n\nThe notes show.\n\n## What / Why / How\n\nThey sit under the run.\n\n**What:** List the notes.\n";
  const theirs = "# Notes show\n\n## Summary\n\nEvery note shows.\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map(other => other.index === -1 ? { ...other, text: "They sit under each run." } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(merge.both).toEqual([{ name: "What / Why / How", theirs: "(removed)", mine: "They sit under each run." }]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe("# Notes show\n\n## Summary\n\nEvery note shows.\n\n## What / Why / How\n\nThey sit under each run.\n");
});

test("my criterion edit with its detail survives on top of a revision that removed that criterion", () => {
  const base = "# Notes show\n\n## Acceptance criteria\n\n- [ ] Each note shows.\n- [ ] A note's author shows.\n  Beside the note.\n";
  const theirs = "# Notes show\n\n## Acceptance criteria\n\n- [ ] Each note shows.\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, criteria: ["Each note shows.", "A note's author shows in bold."], criteriaDetail: ["", "  Under the note."] };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(merge.both).toEqual([{ name: "AC2", theirs: "(removed)", mine: "A note's author shows in bold.\n  Under the note." }]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(`${theirs}- [ ] A note's author shows in bold.\n  Under the note.\n`);
});

test("my unlabelled What / Why / How text replaces the old text in a revision that holds the section as plain text", () => {
  const base = "# Notes show\n\n## What / Why / How\n\nThey sit under the run.\n\n**What:** List the notes.\n";
  const theirs = base.replace("They sit", "<!-- where -->\nThey sit");
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map(other => other.index === -1 ? { ...other, text: "They sit under each run." } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect([merge.onlyMine, merge.both]).toEqual([["What / Why / How"], []]);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(theirs.replace("under the run", "under each run"));
});

test("my criterion and its detail survive on top of a revision that removed every criterion", () => {
  const base = "# Notes show\n\n## Summary\n\nThe notes show.\n\n## Acceptance criteria\n\n- [ ] Each note shows.\n  Beside the run.\n";
  const theirs = "# Notes show\n\n## Summary\n\nEvery note shows.\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, criteria: ["Each note shows in bold."], criteriaDetail: ["  Under the run."] };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  const rebased = composeEdit(theirs, theirFields, merge.onTop);
  expect(rebased).toContain("## Acceptance criteria\n\n- [ ] Each note shows in bold.\n  Under the run.\n");
  expect([loadEdit(rebased, null).criteria, loadEdit(rebased, null).criteriaDetail]).toEqual([["Each note shows in bold."], ["  Under the run."]]);
});

test("my edited introduction replaces theirs once in a plain-text What / Why / How that also holds a Blast radius paragraph", () => {
  const base = "# Notes show\n\n## What / Why / How\n\nThey sit under the run.\n\n**What:** List the notes.\n\n**Blast radius:** The run page only.\n";
  const theirs = base.replace("**What:**", "<!-- where -->\n**What:**");
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map(other => other.index === -1 ? { ...other, text: other.text.replace("under the run", "under each run") } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(theirs.replace("under the run", "under each run"));
});

test("my edited introduction lands once beside their changed Blast radius paragraph in a plain-text What / Why / How", () => {
  const base = "# Notes show\n\n## What / Why / How\n\nThey sit under the run.\n\n**What:** List the notes.\n\n**Blast radius:** The run page only.\n";
  const theirs = base.replace("**What:**", "<!-- where -->\n**What:**").replace("The run page only.", "The run and fleet pages.");
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map(other => other.index === -1 ? { ...other, text: other.text.replace("under the run", "under each run") } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe(theirs.replace("under the run", "under each run"));
});

test("my edited introduction comes back on top of a revision that removed it from a plain-text What / Why / How", () => {
  const base = "# Notes show\n\n## What / Why / How\n\nThey sit under the run.\n\n**What:** List the notes.\n\n**Blast radius:** The run page only.\n";
  const theirs = "# Notes show\n\n## What / Why / How\n\n**What:** List the notes.\n\n<!-- where -->\n**Blast radius:** The run page only.\n";
  const read = loadEdit(base, null);
  const mine: EditFields = { ...read, others: read.others.map(other => other.index === -1 ? { ...other, text: other.text.replace("under the run", "under each run") } : other) };
  const theirFields = loadEdit(theirs, null);
  const merge = mergeEdit(read, theirFields, mine);
  expect(composeEdit(theirs, theirFields, merge.onTop)).toBe("# Notes show\n\n## What / Why / How\n\nThey sit under each run.\n\n**What:** List the notes.\n\n<!-- where -->\n**Blast radius:** The run page only.\n");
});

test("the changed-field names count a removed section and skip one that only moved", () => {
  const base = "# Notes show\n\n## Summary\n\nThe notes show.\n\n## Evidence\n\n- 01-notes.png\n";
  const read = loadEdit(base, null);
  expect(changedNames(read, loadEdit("# Notes show\n\n## Summary\n\nThe notes show.\n", null))).toEqual(["Evidence"]);
  expect(changedNames(read, loadEdit(base.replace("## Evidence", "## Extra\n\nMore.\n\n## Evidence"), null))).toEqual(["Extra"]);
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

async function conflicted(save = () => new Response("unexpected", { status: 500 })) {
  await mount(save);
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
  await conflicted(() => Response.json({ ticket: "DEMO-2", revision: 5 }));
  await press("Put mine on top of revision 4");
  expect(toast()).toBe("Revision 4 loaded with your edits on top. Review, then save as revision 5.");
  expect(banner()).toBeNull();
  expect(header()).toBe("revision 4 + your edit");
  expect([box("AC2").value, box("AC3").value]).toEqual([MY_AC2, THEIR_AC3]);
  expect([box("AC2").closest("[data-changed]") != null, box("AC3").closest("[data-changed]") != null]).toEqual([true, false]);
  expect(saveButton().textContent).toBe("Save revision 5");
  served = { ...REVISION_4, current: { ...REVISION_4.current!, revision: 5 } };
  await act(async () => void fireEvent.click(saveButton()));
  await flush();
  expect(puts).toEqual([{ ifMatch: "4", body: THEIR_BODY.replace(THEIR_AC2, MY_AC2) }]);
  expect(toast()).toBe("Saved DEMO-2 as revision 5.");
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-2");
});

test("on top of a revision that holds Summary as plain text, the Summary shown is the one Save sends", async () => {
  await mount(() => Response.json({ ticket: "DEMO-2", revision: 5 }));
  await type("Summary", "My notes show.");
  const theirBody = BODY.replace("The run page lists each steer note the operator left.", "<!-- one line -->\nTheir notes show.");
  await poll(demo2(4, theirBody, "cli"));
  await press("Put mine on top of revision 4");
  expect(box("Summary").value).toBe("<!-- one line -->\nMy notes show.");
  await type("Summary", "<!-- one line -->\nOur notes show.");
  await act(async () => void fireEvent.click(saveButton()));
  await flush();
  expect(puts).toEqual([{ ifMatch: "4", body: theirBody.replace("Their notes show.", "Our notes show.") }]);
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
