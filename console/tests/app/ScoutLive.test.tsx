import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { ScoutDetail, ScoutTask, ThreadMessage } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const served = (await Bun.file(new URL("scout.json", SERVE)).json()) as ScoutDetail;
const [servedMessage] = served.thread;
const [servedTask] = served.tasks;
const servedReport = served.reports[0]!;

const at = (hour: number, minute: number, second = 0) => new Date(2026, 9, 15, hour, minute, second).getTime();
const ASK = "Why do loop runs crash with database is locked?";
const STEER = "check the digest email too";

const task = (id: number, question: string, phase: ScoutTask["phase"], fields: Partial<ScoutTask> = {}): ScoutTask => ({
  ...servedTask!, id, question, phase, pass: 1, wave: 1, route: "luna", mode: "read", tokens: null,
  started_ms: phase === "queued" ? null : at(11, 47, 12), ended_ms: null, duration_ms: null, ...fields,
});
const RUNNING = task(7, "Repro: two loop workers claim at once", "running");
const QUEUED = task(8, "Does the digest open its own connection?", "queued");
const BACK = task(9, "Where does the loop open SQLite connections?", "done", { ended_ms: at(11, 49), duration_ms: 108_000 });

const swarming: ScoutDetail = {
  ...served,
  scout: {
    ...served.scout, id: 31, ask: ASK, kind: "bug", phase: "swarming", reason: null, lead: "opus", helper: "luna", swarm: 4,
    created_ms: at(11, 45), started_ms: at(11, 46), ended_ms: null, budget: { ...served.scout.budget, minutes: 30, waves: 2 },
  },
  tasks: [RUNNING, QUEUED, BACK],
  reports: [],
  actions: [],
  events: [{ id: 1, at: at(11, 47), kind: "agent_turn", summary: "scout-lead turn ended" }],
  thread: [],
};

const message = (id: number, fields: Partial<ThreadMessage>): ThreadMessage => ({
  ...servedMessage!, id, author: "maintainer", blocks: [], answers: null, consumed_by: null, consumed_ms: null, ...fields,
});
const steer = message(4, { mode: "steer", text: STEER, at: at(11, 50) });
const ask = message(5, { mode: "ask", text: "Does this hit webhooks?", at: at(11, 50) });

interface Daemon {
  scout: ScoutDetail;
  actions?: boolean;
  message?: () => Response;
  stop?: () => Response;
}

/** DEMO answering `/scouts/31` with `daemon.scout`, read at each request, and its message and stop posts; every request is kept. */
function scoutFetch(daemon: Daemon) {
  const seen: { url: string; method: string; body: unknown }[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fetch: Fetch = async (url, init) => {
    seen.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? JSON.parse(init.body) : null });
    if (url === `${DEMO}/scouts/31`) return Response.json(daemon.scout);
    if (url === `${DEMO}/actions/scout-message` && daemon.message) return daemon.message();
    if (url === `${DEMO}/actions/scout-stop` && daemon.stop) return daemon.stop();
    const answer = await polls(url);
    if (!url.endsWith("/status")) return answer;
    return Response.json({ ...(await answer.json()), actions: daemon.actions ?? true });
  };
  return { fetch, seen };
}

async function mount(fetch: Fetch) {
  window.location.hash = "#/scouts/demo/31";
  const fake = fakeDeps(fetch);
  fake.clock.now = at(11, 53);
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
const reread = async (queries: Awaited<ReturnType<typeof mount>>) => {
  await act(async () => {
    await queries.refetchQueries();
  });
  await settled();
};

const region = () => screen.getByRole("complementary", { name: "Thread" });
const rows = () => Array.from(within(region()).getByRole("list", { name: "Thread" }).children, (li) =>
  li.hasAttribute("data-thread-line") ? li.textContent : li.firstElementChild!.textContent);
const send = () => within(region()).getByRole("button", { name: "Send" }) as HTMLButtonElement;
const composeLine = () => region().querySelector("[data-compose-line]")!.textContent;
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
type Seen = { url: string; method: string; body: unknown }[];
const posts = (seen: Seen, route: string) => seen.filter((each) => each.method === "POST" && each.url === `${DEMO}${route}`);

const helperRows = () => Array.from(document.querySelectorAll<HTMLElement>("[data-helper]"));
const rowOf = (question: string) => helperRows().find((li) => li.querySelector("[data-question]")!.textContent === question)!;
const stopButton = (li: HTMLElement) => within(li).queryByRole("button", { name: "Stop this branch" }) as HTMLButtonElement | null;
const lastFact = (li: HTMLElement) => li.querySelector("[data-tokens]")!.textContent;
const openEveryGroup = async () => {
  for (const fold of Array.from(document.querySelectorAll("[aria-expanded='false']"))) await click(fold);
};

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

test("a swarming scout with no thread holds the Thread, an enabled composer and Ask or steer, its line by mode", async () => {
  await mount(scoutFetch({ scout: swarming }).fetch);
  const panel = within(region());
  expect(screen.getByRole("link", { name: "Ask or steer" })).toBeTruthy();
  expect((panel.getByRole("textbox", { name: "Question for the scout" }) as HTMLTextAreaElement).disabled).toBe(false);
  await click(panel.getByRole("button", { name: "Steer" }));
  expect(composeLine()).toBe("Taken at the next wave, or as pass 2.");
  await click(panel.getByRole("button", { name: "Ask" }));
  expect(composeLine()).toBe("Answered when the scout is done.");
});

test("a steer sent to a swarming scout posts once, waits for the next wave, and reads as taken once a wave takes it", async () => {
  const daemon: Daemon = { scout: swarming };
  daemon.message = () => {
    daemon.scout = { ...swarming, thread: [steer] };
    return Response.json({ ok: true, queued: true, detail: "scout 31 is swarming; message 4 waits for its next wave or its end" });
  };
  const { fetch, seen } = scoutFetch(daemon);
  const queries = await mount(fetch);
  await click(within(region()).getByRole("button", { name: "Steer" }));
  await act(async () => void fireEvent.change(within(region()).getByRole("textbox", { name: "Note to the scout" }), { target: { value: STEER } }));
  await click(send());
  expect(posts(seen, "/actions/scout-message").map((each) => each.body)).toEqual([{ scout: 31, mode: "steer", text: STEER }]);
  expect(rows()).toEqual(["You · steer · 11:50", "waiting for the next wave or the end"]);
  daemon.scout = {
    ...swarming,
    thread: [{ ...steer, consumed_by: 12, consumed_ms: at(11, 52) }],
    events: [...swarming.events, { id: 12, at: at(11, 52), kind: "scout_steer_taken", summary: "steer taken for wave 2" }],
  };
  await reread(queries);
  expect(rows()).toEqual(["You · steer · 11:50", "steer taken for wave 2 · 11:52"]);
});

test("a failed scout's composer is disabled with its phase and its untaken ask waits for nothing; a done scout's waits for a slot", async () => {
  await mount(scoutFetch({ scout: { ...swarming, scout: { ...swarming.scout, phase: "failed" }, thread: [ask] } }).fetch);
  expect(send().disabled).toBe(true);
  expect(send().title).toBe("Scout 31 is failed.");
  expect(rows()).toEqual(["You · ask · 11:50"]);
  cleanup();
  const done = { ...swarming, scout: { ...swarming.scout, phase: "done" as const, ended_ms: at(11, 51) }, reports: [servedReport], thread: [ask] };
  await mount(scoutFetch({ scout: done }).fetch);
  expect(rows()).toEqual(["You · ask · 11:50", "waiting for a scout slot"]);
});

test("Stop this branch on a running helper posts its task, reads stopping through a fold, then stopped once the daemon says so", async () => {
  const daemon: Daemon = { scout: swarming, stop: () => Response.json({ ok: true, detail: "stopping task 7 of scout 31" }) };
  const { fetch, seen } = scoutFetch(daemon);
  const queries = await mount(fetch);
  expect([RUNNING, QUEUED, BACK].map((each) => stopButton(rowOf(each.question)) != null)).toEqual([true, true, false]);
  await click(stopButton(rowOf(RUNNING.question))!);
  expect(posts(seen, "/actions/scout-stop").map((each) => each.body)).toEqual([{ scout: 31, task: RUNNING.id }]);
  expect(lastFact(rowOf(RUNNING.question))).toBe("stopping");
  const wave = document.querySelector("[data-group='wave'] [aria-expanded]")!;
  await click(wave);
  await click(wave);
  expect(lastFact(rowOf(RUNNING.question))).toBe("stopping");
  expect(stopButton(rowOf(RUNNING.question))).toBeNull();
  daemon.scout = { ...swarming, tasks: [{ ...RUNNING, phase: "stopped", ended_ms: at(11, 53) }, QUEUED, BACK] };
  await reread(queries);
  const stopped = rowOf(RUNNING.question);
  expect(lastFact(stopped)).toBe("stopped");
  expect(stopped.querySelector("[data-question]")!.classList.contains("line-through")).toBe(true);
  expect(stopButton(stopped)).toBeNull();
});

test("a refused Stop this branch says its detail in the toast and can be pressed again", async () => {
  await mount(scoutFetch({ scout: swarming, stop: () => Response.json({ ok: false, detail: "task 7 is done" }) }).fetch);
  await click(stopButton(rowOf(RUNNING.question))!);
  expect(toast()).toBe("task 7 is done");
  expect(stopButton(rowOf(RUNNING.question))!.disabled).toBe(false);
  expect(lastFact(rowOf(RUNNING.question))).toBe("…");
});

test("a synthesizing or done scout's helpers have no Stop this branch, and with actions off it is disabled", async () => {
  for (const phase of ["synthesizing", "done"] as const) {
    await mount(scoutFetch({ scout: { ...swarming, scout: { ...swarming.scout, phase } } }).fetch);
    await openEveryGroup();
    expect(rowOf(QUEUED.question)).toBeTruthy();
    expect(helperRows().map(stopButton)).toEqual([null, null, null]);
    cleanup();
  }
  await mount(scoutFetch({ scout: swarming, actions: false }).fetch);
  const button = stopButton(rowOf(RUNNING.question))!;
  expect([button.disabled, button.title]).toEqual([true, ACTIONS_OFF]);
});
