import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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
const title = "Steer notes show on the run detail card";
const key = "holo.newTicket.demo";
const values = {
  Title: title,
  Summary: "The run page lists each steer note.",
  What: "Show the notes beside the run.",
  Why: "Keep the operator informed.",
  How: "Read the run's notes.",
  "In scope": "The run card\nIts styles",
  "Out of scope": "Editing notes",
  "Verify commands": "ruff check .\nbun --cwd=console test",
  "Implementation notes": "Use the existing notes query.\nKeep the existing ordering.",
};

function fleet(answer: Fetch = async () => new Response(null, { status: 500 })): Fetch {
  const daemon = (project: string) => ({
    status: { ...working, project: `/srv/dev/${project}`, actions: true, runs: [], queued: [], now: NOW },
    attention: { level: "none", now: NOW, project: `/srv/dev/${project}`, items: [] },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("demo"), [WRITER]: daemon("relos") });
  return async (url, init) => {
    if (url === `${DEMO}/tickets`) return answer(url, init);
    if (url === `${DEMO}/board` || url === `${WRITER}/board`) return Response.json({ ...board, editable: true });
    if (url === `${DEMO}/tickets/DEMO-12`) return Response.json({ ...detail, ticket: "DEMO-12", title });
    return polls(url);
  };
}

async function flush() {
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function mount(project = "demo", fetch: Fetch = fleet()) {
  window.location.hash = `#/tickets/${project}/new`;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  await flush();
}

async function press(name: string) {
  await act(async () => void fireEvent.click(screen.getByRole("button", { name })));
  await flush();
}

async function fill() {
  for (const [name, value] of Object.entries(values)) {
    await act(async () => void fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } }));
  }
  await act(async () => void fireEvent.change(screen.getByRole("combobox", { name: "Priority" }), { target: { value: "1" } }));
}

function expectBlank() {
  for (const name of Object.keys(values)) expect((screen.getByRole("textbox", { name }) as HTMLInputElement).value).toBe("");
  expect((screen.getByRole("combobox", { name: "Priority" }) as HTMLSelectElement).value).toBe("");
  expect(screen.queryByText(/^Draft from .* restored\.$/)).toBeNull();
  expect((screen.getByRole("button", { name: "Save draft" }) as HTMLButtonElement).disabled).toBe(true);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
const storage = globalThis.localStorage;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { sessionStorage.clear(); storage.clear(); });
afterEach(() => {
  cleanup();
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: storage });
  window.location.hash = "";
});

test("Save draft restores every field, P1 and the saved time only in its project after reopening", async () => {
  await mount();
  expectBlank();
  await fill();
  const beforeSave = Date.now();
  await press("Save draft");
  const afterSave = Date.now();
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Draft kept in this browser.");
  const draft = JSON.parse(storage.getItem(key)!) as { savedAt: number };
  expect(draft.savedAt).toBeGreaterThanOrEqual(beforeSave);
  expect(draft.savedAt).toBeLessThanOrEqual(afterSave);
  cleanup();
  await mount("relos");
  expectBlank();
  cleanup();
  await mount();
  for (const [name, value] of Object.entries(values)) expect((screen.getByRole("textbox", { name }) as HTMLInputElement).value).toBe(value);
  expect((screen.getByRole("combobox", { name: "Priority" }) as HTMLSelectElement).value).toBe("1");
  const time = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false }).format(draft.savedAt);
  expect(screen.getByText(`Draft from ${time} restored.`)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Start over" })).toBeTruthy();
});

test("switching projects clears fields and priority without a draft and restores the saved project's draft", async () => {
  await mount();
  await fill();
  await press("Save draft");
  await act(async () => void fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "relos" } }));
  await flush();
  expect(window.location.hash).toBe("#/tickets/relos/new");
  expectBlank();
  await act(async () => void fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "demo" } }));
  await flush();
  expect(window.location.hash).toBe("#/tickets/demo/new");
  for (const [name, value] of Object.entries(values)) expect((screen.getByRole("textbox", { name }) as HTMLInputElement).value).toBe(value);
  expect((screen.getByRole("combobox", { name: "Priority" }) as HTMLSelectElement).value).toBe("1");
  expect(screen.getByText(/^Draft from .* restored\.$/)).toBeTruthy();
});

test("Save draft preserves paragraphs and fenced field labels across repeated saves", async () => {
  const prose = {
    What: "Show the notes.\n\nPreserve their order.",
    Why: "Keep the operator informed.\n\n- Show progress\n- Keep context\n\n   ~~~~markdown\n**What:** example\n~~~\n**How:** still fenced\n   ~~~~\nAfter the example.",
    How: "Read the notes.\n\n1. Fetch the record\n2. Render each note\n\n````markdown\n**Why:** example\n```\n**UI change:** still fenced\n## Example heading\n````\nAfter the example.",
  };
  await mount();
  for (const [name, value] of Object.entries(prose)) {
    await act(async () => void fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } }));
  }
  for (let save = 0; save < 2; save += 1) {
    await press("Save draft");
    cleanup();
    await mount();
    for (const [name, value] of Object.entries(prose)) {
      expect((screen.getByRole("textbox", { name }) as HTMLTextAreaElement).value).toBe(value);
    }
  }
});

test("Save draft preserves Markdown headings in prose across repeated saves", async () => {
  const prose = {
    What: "Plan\n\n## Details\nKeep this",
    Why: "Reason\n\n## Context\nKeep the rationale",
    How: "Method\n\n## Steps\nKeep the instructions",
  };
  await mount();
  for (const [name, value] of Object.entries(prose)) {
    await act(async () => void fireEvent.change(screen.getByRole("textbox", { name }), { target: { value } }));
  }
  for (let save = 0; save < 2; save += 1) {
    await press("Save draft");
    cleanup();
    await mount();
    for (const [name, value] of Object.entries(prose)) {
      expect((screen.getByRole("textbox", { name }) as HTMLTextAreaElement).value).toBe(value);
    }
  }
});

test("Start over clears a restored draft and leaves a blank form on the next opening", async () => {
  await mount();
  await fill();
  await press("Save draft");
  cleanup();
  await mount();
  expect(screen.getByText(/^Draft from .* restored\.$/)).toBeTruthy();
  await press("Start over");
  expectBlank();
  expect(storage.getItem(key)).toBeNull();
  cleanup();
  await mount();
  expectBlank();
});

test("a 201 filing removes a restored draft so opening New ticket again is blank", async () => {
  await mount();
  await fill();
  await press("Save draft");
  cleanup();
  const answer = fakeFetch(() => Response.json({ ticket: "DEMO-12", revision: 1 }, { status: 201 }));
  await mount("demo", fleet(answer.fetchImpl));
  expect(screen.getByText(/^Draft from .* restored\.$/)).toBeTruthy();
  await press("File ticket");
  expect(answer.seen).toHaveLength(1);
  expect(answer.seen[0]!.body).toMatchObject({ priority: 1, column: "backlog" });
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-12");
  expect(storage.getItem(key)).toBeNull();
  cleanup();
  await mount();
  expectBlank();
});

test("blocked browser storage opens blank, explains Save draft failure and still files a ticket", async () => {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => { throw new Error("Site data blocked"); },
      setItem: () => { throw new Error("Site data blocked"); },
      removeItem: () => { throw new Error("Site data blocked"); },
    },
  });
  const answer = fakeFetch(() => Response.json({ ticket: "DEMO-12", revision: 1 }, { status: 201 }));
  await mount("demo", fleet(answer.fetchImpl));
  expectBlank();
  await fill();
  await press("Save draft");
  expect(document.querySelector("[data-toast]")?.textContent).toBe("This browser keeps no drafts.");
  expect((screen.getByRole("textbox", { name: "Title" }) as HTMLInputElement).value).toBe(title);
  await press("File ticket");
  expect(answer.seen).toHaveLength(1);
  expect([answer.seen[0]!.url, answer.seen[0]!.method]).toEqual([`${DEMO}/tickets`, "POST"]);
  expect((answer.seen[0]!.body as { body: string }).body).toContain(`# ${title}\n`);
  expect(document.querySelector("[data-toast]")?.textContent).toBe("Filed DEMO-12 in demo Backlog.");
  expect(window.location.hash).toBe("#/tickets/demo/DEMO-12");
});
