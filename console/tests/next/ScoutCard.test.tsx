import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { ProposalBlock, ScoutDetail, ScoutEvent, ThreadMessage } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const [servedMessage] = served.thread;
const [servedTask] = served.tasks;
const servedReport = served.reports[0]!;
const servedAction = served.actions[0]!;

const at = (hour: number, minute: number, second = 0) => new Date(2026, 9, 15, hour, minute, second).getTime();
const ASK = "Why do loop runs crash with database is locked?";
const QUESTION = "Does F1 also explain the crashes?";

const message = (id: number, fields: Partial<ThreadMessage>): ThreadMessage => ({
  ...servedMessage!, id, blocks: [], answers: null, consumed_by: null, consumed_ms: null, ...fields,
});
const event = (id: number, kind: string, when: number, summary = kind): ScoutEvent => ({ id, at: when, kind, summary });
const report = (pass: number, when: number, findings: number) => ({
  ...servedReport, pass, at: when,
  report: { ...servedReport.report, findings: Array.from({ length: findings }, () => servedReport.report.findings[0]!) },
});

const askMessage = message(1, { author: "maintainer", mode: "ask", text: QUESTION, at: at(14, 40), consumed_by: 3, consumed_ms: at(14, 40, 10) });
const answerMessage = message(2, {
  author: "scout", mode: "answer", answers: 1, at: at(14, 41, 2),
  text: "No. Their crashes come from the digest job, which opens its own connection at `lotuspod/digest.py:57`.",
});
const steerMessage = message(3, {
  author: "test seat via MCP", mode: "steer", text: "Go check whether the scheduled digest takes the same lock.", at: at(14, 44), consumed_by: 5, consumed_ms: at(14, 44, 5),
});

const done: ScoutDetail = {
  ...served,
  scout: {
    ...served.scout, id: 31, ask: ASK, kind: "bug", phase: "done", reason: null, lead: "opus", helper: "luna", swarm: 4,
    created_ms: at(14, 1), started_ms: at(14, 2), ended_ms: at(14, 51, 15),
  },
  tasks: [
    { ...servedTask!, pass: 1, wave: 1, route: "luna", started_ms: at(14, 3), ended_ms: at(14, 10) },
    { ...servedTask!, pass: 2, wave: 1, route: "luna", started_ms: at(14, 44, 30), ended_ms: at(14, 48) },
    { ...servedTask!, pass: 2, wave: 1, route: "luna", started_ms: at(14, 44, 40), ended_ms: at(14, 47) },
  ],
  reports: [report(1, at(14, 35), 4), report(2, at(14, 51, 10), 1)],
  actions: [1, 2, 3, 4, 5].map((position) => ({ ...servedAction, position, pass: position === 5 ? 2 : 1 })),
  events: [
    event(1, "scout_answer_started", at(14, 40, 10)),
    event(2, "scout_answered", at(14, 41, 2)),
    event(3, "scout_pass_started", at(14, 44, 5)),
    event(4, "scout_pass_done", at(14, 51, 15)),
  ],
  thread: [askMessage, answerMessage, steerMessage],
};

const CARD_QUESTION = "Fold F5 into A1, or file it as A5?";
const proposal: ProposalBlock = {
  type: "proposal", question: CARD_QUESTION,
  choices: [{ label: "Fold into A1", mode: "steer" }, { label: "File as A5", mode: "ask" }],
};
const cardMessage = message(9, { author: "scout", mode: "answer", answers: 3, text: CARD_QUESTION, blocks: [proposal], at: at(14, 51, 20) });
const carded: ScoutDetail = { ...done, thread: [...done.thread, cardMessage] };

interface Daemon {
  scout: ScoutDetail;
  actions?: boolean;
  post?: () => Response;
}

/** DEMO answering `/scouts/31` with `daemon.scout` and its message posts with `daemon.post`; every request is kept. */
function scoutFetch(daemon: Daemon) {
  const seen: { url: string; method: string; body: unknown }[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fetch: Fetch = async (url, init) => {
    seen.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    if (url === `${DEMO}/scouts/31`) return Response.json(daemon.scout);
    if (url === `${DEMO}/actions/scout-message` && daemon.post) return daemon.post();
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: daemon.actions ?? true });
  };
  return { fetch, seen };
}

async function mount(fetch: Fetch) {
  window.location.hash = "#/scouts/demo/31";
  const fake = fakeDeps(fetch);
  fake.clock.now = at(15, 0);
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return queries;
}

const settled = async () => {
  for (let round = 0; round < 4; round += 1) await act(settle);
};
const click = async (element: Element) => {
  await act(async () => void fireEvent.click(element));
  await settled();
};
const type = async (box: HTMLElement, text: string) => {
  await act(async () => void fireEvent.change(box, { target: { value: text } }));
};

const region = () => screen.getByRole("complementary", { name: "Thread" });
const list = () => within(region()).getByRole("list", { name: "Thread" });
const items = () => Array.from(list().children) as HTMLElement[];
const send = () => within(region()).getByRole("button", { name: "Send" }) as HTMLButtonElement;
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
type Seen = { url: string; method: string; body: unknown }[];
const posts = (seen: Seen) => seen.filter((each) => each.method === "POST" && each.url === `${DEMO}/actions/scout-message`);
const reads = (seen: Seen) => seen.filter((each) => each.method === "GET" && each.url === `${DEMO}/scouts/31`).length;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

const card = () => items().at(-1)!;
const cardButtons = () => within(card()).queryAllByRole("button") as HTMLButtonElement[];
const choice = (name: string) => within(card()).getByRole("button", { name }) as HTMLButtonElement;
const taken = () => Response.json({ ok: true, queued: false, detail: "message 12 posted" });

test("a scout's proposal card ends the Thread with its head, question and a button per choice, then Type an answer", async () => {
  await mount(scoutFetch({ scout: carded }).fetch);
  expect(card().firstElementChild!.textContent).toBe("Scout · 14:51");
  expect(card().textContent).toContain(CARD_QUESTION);
  expect(cardButtons().map((button) => button.textContent)).toEqual(["Fold into A1", "File as A5", "Type an answer"]);
  expect(within(region()).queryByRole("button", { name: "Dismiss" })).toBeNull();
});

test("a choice posts its label in its own mode as the card's answer, says so and reads the scout again", async () => {
  for (const [name, mode, said] of [["Fold into A1", "steer", "Steered scout 31."], ["File as A5", "ask", "Asked scout 31."]] as const) {
    const daemon = scoutFetch({ scout: carded, post: taken });
    await mount(daemon.fetch);
    const before = reads(daemon.seen);
    await click(choice(name));
    expect(posts(daemon.seen).map((each) => each.body)).toEqual([{ scout: 31, mode, text: name, answers: 9 }]);
    expect(toast()).toBe(said);
    expect(reads(daemon.seen)).toBeGreaterThan(before);
    cleanup();
  }
});

test("a card a later message answers keeps its question, loses its buttons, and the answer follows it", async () => {
  const reply = message(10, { author: "maintainer", mode: "steer", text: "Fold into A1", answers: 9, at: at(14, 53), consumed_by: 11, consumed_ms: at(14, 53, 5) });
  await mount(scoutFetch({ scout: { ...carded, thread: [...carded.thread, reply] } }).fetch);
  const answered = list().querySelector("[data-scout-card]")!;
  expect(answered.textContent).toContain(CARD_QUESTION);
  expect(within(answered as HTMLElement).queryAllByRole("button")).toEqual([]);
  expect(within(region()).queryByRole("button", { name: "Type an answer" })).toBeNull();
  expect(answered.nextElementSibling!.firstElementChild!.textContent).toBe("You · steer · 14:53");
});

test("Type an answer focuses the composer naming the card, and Send posts the typed text as its answer", async () => {
  const daemon = scoutFetch({ scout: carded, post: taken });
  await mount(daemon.fetch);
  await click(choice("Type an answer"));
  const box = within(region()).getByRole("textbox", { name: "Question for the scout" });
  expect(document.activeElement).toBe(box);
  expect(region().querySelector("[data-answering]")!.textContent).toContain(`Answering: ${CARD_QUESTION}`);
  await type(box, "Neither, drop F5");
  await click(send());
  expect(posts(daemon.seen).map((each) => each.body)).toEqual([{ scout: 31, mode: "ask", text: "Neither, drop F5", answers: 9 }]);
  expect(region().querySelector("[data-answering]")).toBeNull();
});

test("Stop answering drops the Answering line and the next Send names no card", async () => {
  const daemon = scoutFetch({ scout: carded, post: taken });
  await mount(daemon.fetch);
  await click(choice("Type an answer"));
  await click(within(region()).getByRole("button", { name: "Stop answering" }));
  expect(region().textContent).not.toContain("Answering:");
  await type(within(region()).getByRole("textbox", { name: "Question for the scout" }), "Neither, drop F5");
  await click(send());
  expect(posts(daemon.seen).map((each) => each.body)).toEqual([{ scout: 31, mode: "ask", text: "Neither, drop F5" }]);
});

test("a refused choice reads its detail under the card and enables the buttons again", async () => {
  const refusal = "message 9 is already answered by message 11";
  await mount(scoutFetch({ scout: carded, post: () => Response.json({ ok: false, detail: refusal }) }).fetch);
  await click(choice("Fold into A1"));
  expect(within(card()).getByRole("alert").textContent).toBe(refusal);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(ASK);
  expect(cardButtons().map((button) => button.disabled)).toEqual([false, false, false]);
});

test("the card's buttons are disabled with the composer's title when actions are off or the scout is answering", async () => {
  await mount(scoutFetch({ scout: carded, actions: false }).fetch);
  expect(cardButtons().map((button) => [button.disabled, button.title])).toEqual(Array(3).fill([true, ACTIONS_OFF]));
  cleanup();
  await mount(scoutFetch({ scout: { ...carded, scout: { ...carded.scout, phase: "answering" } } }).fetch);
  expect(cardButtons().map((button) => [button.disabled, button.title])).toEqual(Array(3).fill([true, "Scout 31 is answering."]));
});
