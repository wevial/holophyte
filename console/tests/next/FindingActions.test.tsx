import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { Findings, FindingsRun, HostStatus, Round, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { HOST_ONLY } from "../../src/next/findings";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, MINUTE, NOW, run, SERVE } from "./needsFleet";

const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const hostStatus = (await Bun.file(new URL("host-status.json", SERVE)).json()) as HostStatus;
const pinned = (await Bun.file(new URL("findings.json", SERVE)).json()) as Findings;
const pinnedRun = pinned.runs[0]!;
const pinnedRound = pinnedRun.rounds[0]!;
const pinnedFinding = pinnedRound.findings[0]!;

const PROJECT = `${DEMO}/projects/demo`;
const AT = NOW - 60 * MINUTE;
const THREAD = "https://github.com/example/repo/pull/519#discussion_r3: Consider a retry cap";

const round = (findings: Round["findings"]): Round => ({
  ...pinnedRound,
  round: 1,
  started_ms: AT,
  ended_ms: AT,
  verdict: "changes_requested",
  reviewer_model: "reviewer",
  findings,
});

const runOf = (id: number, ticket: string, rounds: Round[], facts: Partial<FindingsRun> = {}): FindingsRun => ({
  ...pinnedRun,
  run: id,
  ticket,
  phase: "working",
  outcome: null,
  pr_url: null,
  rounds,
  ledger: [],
  advisories: [],
  ...facts,
});

const flag = { ...pinnedFinding, path: "holophyte/serve/server.py", line: 12, severity: "p2", criterion: null, message: "**Flag is ignored** The detail of it." };
const demo11 = runOf(11, "DEMO-11", [round([flag])], { phase: "failed", outcome: "failed" });
const lease = { ...flag, path: "holophyte/sweep/lease.py", line: 142, message: "**Lease never expires** The detail." };
const demo7 = runOf(7, "DEMO-7", [round([lease])]);
const reviewing7 = [{ ...run(7, "DEMO-7"), phase: "reviewing", round: 1, max_rounds: 3, heartbeat_age_ms: 30_000 }];
const holo229 = runOf(229, "HOLO-229", [], {
  outcome: "merged",
  pr_url: "https://github.com/example/repo/pull/519",
  advisories: [{ tone: "advisory", message: THREAD }],
});

interface Post {
  url: string;
  body: Record<string, unknown>;
}

/** DEMO as a host daemon serving `demo` with `runs` on `/findings`, or with
 *  `projectDaemon` the project's own daemon at DEMO; each POST recorded in
 *  `posts` and answered by `answer`. */
function daemon(opts: { runs: FindingsRun[]; actions: boolean; live?: Status["runs"]; projectDaemon?: boolean; answer?: (post: Post) => Promise<Response> }) {
  const posts: Post[] = [];
  const base = opts.projectDaemon ? DEMO : PROJECT;
  const status = { ...working, project: "/srv/dev/demo", actions: opts.actions, runs: opts.live ?? [], queued: [], now: NOW };
  const listed = [{ ...hostStatus.projects[0]!, name: "demo", path: "/srv/dev/demo", host: "writer-host" }];
  const fetch: Fetch = async (url, init) => {
    if (init?.method === "POST") {
      const post = { url, body: JSON.parse(String(init.body)) as Record<string, unknown> };
      posts.push(post);
      return opts.answer!(post);
    }
    if (url === `${DEMO}/peers`) return Response.json({ self: "writer-host:7710", peers: [] });
    if (!opts.projectDaemon && url === `${DEMO}/status`) return Response.json({ ...hostStatus, now: NOW, projects: listed });
    if (!opts.projectDaemon && url === `${DEMO}/attention`) return Response.json({ level: "none", now: NOW, items: [] });
    if (url === `${base}/status`) return Response.json(status);
    if (url === `${base}/attention`) return Response.json({ level: "none", now: NOW, project: "/srv/dev/demo", items: [] });
    if (url.startsWith(`${base}/findings?`)) return Response.json({ ...pinned, since: 0, runs: opts.runs, follow_ups: [] });
    return new Response("not found", { status: 404 });
  };
  return { fetch, posts };
}

async function mount(fetchImpl: Fetch) {
  window.location.hash = "#/findings";
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let step = 0; step < 6; step += 1) await act(settle);
}

const rowOf = (group: string, text: string) =>
  [...screen.getByRole("region", { name: group }).querySelectorAll<HTMLElement>("[data-finding-row]")].find((row) => row.textContent!.includes(text))!;

function unfold(group: string, text: string): HTMLElement {
  const row = rowOf(group, text);
  fireEvent.click(row.querySelector<HTMLButtonElement>("button[aria-expanded]")!);
  return row;
}

const buttonOf = (row: HTMLElement, label: RegExp) => [...row.querySelectorAll<HTMLButtonElement>("button.btn")].find((each) => label.test(each.textContent!));
const fateOf = (row: HTMLElement) => row.querySelector("[data-fate]")!.textContent;
const toast = () => document.querySelector("[data-toast]")!.textContent;

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => sessionStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("Steer into next run posts the open finding as DEMO-11's amendment, reads Steering… while held, then stamps the steered fate and toasts", async () => {
  let release: (response: Response) => void = () => {};
  const held = new Promise<Response>((resolve) => (release = resolve));
  const { fetch, posts } = daemon({ runs: [demo11], actions: true, answer: () => held });
  await mount(fetch);
  const row = unfold("Open", "Flag is ignored");
  expect(fateOf(row)).toBe("open · the run failed, so nobody is fixing it");

  await act(async () => {
    fireEvent.click(buttonOf(row, /^Steer into next run$/)!);
    await settle();
  });
  expect(posts).toHaveLength(1);
  expect(posts[0]!.url).toBe(`${PROJECT}/actions/steer`);
  expect(posts[0]!.body.ticket).toBe("DEMO-11");
  expect(posts[0]!.body.hint).toBe(false);
  expect(String(posts[0]!.body.note).startsWith("Review finding from round 1, still open: holophyte/serve/server.py:12")).toBe(true);
  expect(buttonOf(row, /^Steering…$/)).toBeDefined();

  await act(async () => {
    release(Response.json({ action: "steer", ok: true, ticket: "DEMO-11", detail: "DEMO-11 steered" }));
    await settle();
  });
  for (let step = 0; step < 3; step += 1) await act(settle);
  expect(fateOf(row)).toBe("steered · DEMO-11 reads it on its next run");
  expect(row.querySelector("[data-fate] .stamp")).not.toBeNull();
  expect(buttonOf(row, /Steer/)).toBeUndefined();
  expect(toast()).toBe("Steered DEMO-11 with the finding.");
});

test("a refused steer keeps the button enabled and the fate line, and says the daemon's reason", async () => {
  const { fetch } = daemon({ runs: [demo11], actions: true, answer: async () => Response.json({ ok: false, detail: "DEMO-11 is held" }) });
  await mount(fetch);
  const row = unfold("Open", "Flag is ignored");
  await act(async () => {
    fireEvent.click(buttonOf(row, /^Steer into next run$/)!);
    await settle();
  });
  for (let step = 0; step < 3; step += 1) await act(settle);
  const button = buttonOf(row, /^Steer into next run$/)!;
  expect(button.disabled).toBe(false);
  expect(fateOf(row)).toBe("open · the run failed, so nobody is fixing it");
  expect(toast()).toBe("DEMO-11 is held");
});

test("File ticket files the advisory thread to Backlog on a host daemon, and a 422 says its first problem and enables the button again", async () => {
  const answers = [
    Response.json({ problems: ["Missing section: Acceptance criteria", "Missing estimate"] }, { status: 422 }),
    Response.json({ ticket: "HOLO-248", revision: 1 }, { status: 201 }),
  ];
  const { fetch, posts } = daemon({ runs: [holo229], actions: true, answer: async () => answers.shift()! });
  await mount(fetch);
  const row = unfold("Follow-ups", "Consider a retry cap");
  expect(fateOf(row)).toBe("noted, not acted on");

  const press = async () => {
    await act(async () => {
      fireEvent.click(buttonOf(row, /^File ticket$/)!);
      await settle();
    });
    for (let step = 0; step < 3; step += 1) await act(settle);
  };
  await press();
  expect(toast()).toBe("Missing section: Acceptance criteria");
  expect(buttonOf(row, /^File ticket$/)!.disabled).toBe(false);
  expect(fateOf(row)).toBe("noted, not acted on");

  await press();
  expect(posts).toHaveLength(2);
  expect(posts[1]!.url).toBe(`${PROJECT}/tickets`);
  expect(posts[1]!.body.column).toBe("backlog");
  const body = String(posts[1]!.body.body).split("\n");
  expect(body[0]).toBe("# Consider a retry cap");
  expect(body).toContain("Raised as an advisory thread on PR #519 for HOLO-229.");
  expect(fateOf(row)).toBe("filed as HOLO-248 · Backlog");
  expect(buttonOf(row, /File ticket/)).toBeUndefined();
  expect(toast()).toBe("Filed HOLO-248 in Backlog.");
});

test("with actions off both buttons draw disabled under ACTIONS_OFF and post nothing, and a finding being fixed has no button", async () => {
  const { fetch, posts } = daemon({ runs: [demo11, demo7, holo229], actions: false, live: reviewing7 });
  await mount(fetch);
  const steer = buttonOf(unfold("Open", "Flag is ignored"), /^Steer into next run$/)!;
  const file = buttonOf(unfold("Follow-ups", "Consider a retry cap"), /^File ticket$/)!;
  for (const button of [steer, file]) {
    expect(button.disabled).toBe(true);
    expect(button.title).toBe(ACTIONS_OFF);
    fireEvent.click(button);
  }
  await act(settle);
  expect(posts).toHaveLength(0);
  expect(buttonOf(unfold("Open", "Lease never expires"), /./)).toBeUndefined();
});

test("on a project daemon with actions on File ticket draws disabled while Steer into next run stays enabled", async () => {
  const { fetch } = daemon({ runs: [demo11, holo229], actions: true, projectDaemon: true });
  await mount(fetch);
  expect(buttonOf(unfold("Open", "Flag is ignored"), /^Steer into next run$/)!.disabled).toBe(false);
  const file = buttonOf(unfold("Follow-ups", "Consider a retry cap"), /^File ticket$/)!;
  expect(file.disabled).toBe(true);
  expect(file.title).toBe(HOST_ONLY);
});
