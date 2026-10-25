import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { ScoutDetail, ScoutEvent, ThreadMessage } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
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
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
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
/** Each row as it reads: a message's head, or a line's text. */
const rows = () => items().map((li) => (li.hasAttribute("data-thread-line") ? li.textContent : li.firstElementChild!.textContent));
const send = () => within(region()).getByRole("button", { name: "Send" }) as HTMLButtonElement;
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
type Seen = { url: string; method: string; body: unknown }[];
const posts = (seen: Seen) => seen.filter((each) => each.method === "POST" && each.url === `${DEMO}/actions/scout-message`);
const reads = (seen: Seen) => seen.filter((each) => each.method === "GET" && each.url === `${DEMO}/scouts/31`).length;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a done scout's Thread reads its lead and its messages in order, each headed by its author, mode and time", async () => {
  await mount(scoutFetch({ scout: done }).fetch);
  expect(region().querySelector("[data-thread-lead]")!.textContent).toBe("lead · opus");
  const messages = items().filter((li) => !li.hasAttribute("data-thread-line"));
  expect(messages.map((li) => li.firstElementChild!.textContent)).toEqual(["You · ask · 14:40", "Scout · 14:41", "test seat via MCP · steer · 14:44"]);
  expect(messages[0]!.textContent).toContain(QUESTION);
  expect(messages[1]!.querySelector("code")!.textContent).toBe("lotuspod/digest.py:57");
});

test("an answer turn and a pass read as lines where their time falls, and a failed pass reads its summary", async () => {
  await mount(scoutFetch({ scout: done }).fetch);
  expect(rows()).toEqual([
    "You · ask · 14:40",
    "answer turn · opus · 52s",
    "Scout · 14:41",
    "test seat via MCP · steer · 14:44",
    "pass 2 · luna ×2 · 14:44",
    "pass 2 done · 7m · F5, A5 · 14:51",
  ]);
  cleanup();
  const failed = { ...done, events: [...done.events, event(5, "scout_pass_failed", at(14, 58), "report still malformed after its repair: summary")] };
  await mount(scoutFetch({ scout: failed }).fetch);
  const last = items().at(-1)!;
  expect(last.textContent).toBe("report still malformed after its repair: summary");
  expect(last.hasAttribute("data-thread-line")).toBe(true);
});

test("an answering scout's newest line runs now, its header says the lead is answering, and Send waits", async () => {
  const ask = message(4, { author: "maintainer", mode: "ask", text: "And the recurring tasks?", at: at(14, 55), consumed_by: 9, consumed_ms: at(14, 55, 5) });
  const answering: ScoutDetail = {
    ...done, scout: { ...done.scout, phase: "answering" },
    events: [...done.events, event(9, "scout_answer_started", at(14, 55, 5))], thread: [...done.thread, ask],
  };
  await mount(scoutFetch({ scout: answering }).fetch);
  expect(rows().at(-1)).toBe("answer turn · opus · now");
  expect(document.querySelector("header [role='status']")!.textContent).toBe("The lead is answering.");
  expect(screen.getByRole("group", { name: "Stage" }).textContent).toBe("answering");
  expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  expect(screen.queryByRole("link", { name: "Ask or steer" })).toBeNull();
  expect(send().disabled).toBe(true);
  expect(send().title).toBe("Scout 31 is answering.");
});

test("an ask no turn has taken reads as waiting for a slot, and Send stays disabled with text typed", async () => {
  const waiting = { ...done, thread: [...done.thread, message(7, { author: "maintainer", mode: "ask", text: "Does this hit webhooks?", at: at(14, 58) })] };
  await mount(scoutFetch({ scout: waiting }).fetch);
  expect(rows().slice(-2)).toEqual(["You · ask · 14:58", "waiting for a scout slot"]);
  await type(within(region()).getByRole("textbox", { name: "Question for the scout" }), "One more question");
  expect(send().disabled).toBe(true);
  expect(send().title).toBe("Message 7 waits for a scout slot.");
});

test("Send posts the trimmed ask, says so, clears the box and reads the scout again", async () => {
  const daemon = scoutFetch({ scout: done, post: () => Response.json({ ok: true, queued: false, detail: "asked scout 31" }) });
  await mount(daemon.fetch);
  const box = within(region()).getByRole("textbox", { name: "Question for the scout" }) as HTMLTextAreaElement;
  await type(box, "  Does this hit recurring tasks too?  ");
  const before = reads(daemon.seen);
  await click(send());
  expect(posts(daemon.seen).map((each) => each.body)).toEqual([{ scout: 31, mode: "ask", text: "Does this hit recurring tasks too?" }]);
  expect(toast()).toBe("Asked scout 31.");
  expect(box.value).toBe("");
  expect(reads(daemon.seen)).toBeGreaterThan(before);
});

test("Steer renames the box, names the next pass and posts a steer", async () => {
  const daemon = scoutFetch({ scout: done, post: () => Response.json({ ok: true, queued: false, detail: "steered scout 31" }) });
  await mount(daemon.fetch);
  const steer = within(region()).getByRole("button", { name: "Steer" });
  await click(steer);
  expect(steer.getAttribute("aria-pressed")).toBe("true");
  const box = within(region()).getByRole("textbox", { name: "Note to the scout" });
  expect(region().querySelector("[data-compose-line]")!.textContent).toBe("Starts pass 3.");
  await type(box, "Go check the digest email as well");
  await click(send());
  expect(posts(daemon.seen).map((each) => each.body)).toEqual([{ scout: 31, mode: "steer", text: "Go check the digest email as well" }]);
  expect(toast()).toBe("Steered scout 31.");
});

test("a refused or rejected message reads under the composer and keeps its text", async () => {
  const answers = [
    () => Response.json({ ok: false, detail: "scout 31 is swarming" }),
    () => Response.json({ error: "text must be a non-blank string" }, { status: 400 }),
  ];
  let next = 0;
  await mount(scoutFetch({ scout: done, post: () => answers[next++]!() }).fetch);
  const box = within(region()).getByRole("textbox", { name: "Question for the scout" }) as HTMLTextAreaElement;
  await type(box, "Does this hit recurring tasks too?");
  await click(send());
  expect(within(region()).getByRole("alert").textContent).toBe("scout 31 is swarming");
  await click(send());
  expect(within(region()).getByRole("alert").textContent).toContain("text must be a non-blank string");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(ASK);
  expect(box.value).toBe("Does this hit recurring tasks too?");
});

test("with actions off the composer is disabled, and a failed scout with no thread has no Thread", async () => {
  await mount(scoutFetch({ scout: done, actions: false }).fetch);
  const panel = within(region());
  for (const name of ["Send", "Ask", "Steer"]) expect((panel.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
  expect((panel.getByRole("textbox", { name: "Question for the scout" }) as HTMLTextAreaElement).disabled).toBe(true);
  expect(send().title).toBe(ACTIONS_OFF);
  cleanup();
  await mount(scoutFetch({ scout: { ...done, scout: { ...done.scout, phase: "failed" }, thread: [] } }).fetch);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(ASK);
  expect(screen.queryByRole("complementary", { name: "Thread" })).toBeNull();
});

test("Ask or steer moves focus to the composer's box", async () => {
  await mount(scoutFetch({ scout: done }).fetch);
  await click(screen.getByRole("link", { name: "Ask or steer" }));
  expect(document.activeElement).toBe(within(region()).getByRole("textbox", { name: "Question for the scout" }));
});
