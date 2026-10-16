import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunDetail, Status, TicketDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, MINUTE, NOW, run, SERVE } from "./needsFleet";

const servedTicket = (await Bun.file(new URL("ticket.json", SERVE)).json()) as TicketDetail;

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;

const detail = (id: number, ticket: string, facts: Partial<RunDetail["run"]>): RunDetail => ({
  ...servedRun,
  chain: { ...servedRun.chain!, runs: [{ ...servedRun.chain!.runs[0]!, id: id - 1 }, { ...servedRun.chain!.runs[0]!, id }] },
  run: { ...servedRun.run, id, ticket, title: `${ticket} title`, branch: `task/${ticket.toLowerCase()}-x`, pr_url: null, ...facts },
});

const runs: Record<number, RunDetail> = {
  12: detail(12, "DEMO-12", { phase: "reviewing", pr_url: "https://github.com/acme/demo/pull/524" }),
  13: detail(13, "DEMO-13", { phase: "implementing" }),
  39: detail(39, "DEMO-40", { phase: "failed", outcome: "failed", ended_ms: NOW - 20 * MINUTE }),
  40: detail(40, "DEMO-40", { phase: "failed", outcome: "failed", failure_kind: "verify", ended_ms: NOW - 10 * MINUTE }),
  9: detail(9, "DEMO-9", { phase: "done", outcome: "merged", merge_sha: "5acc1384b2e0", ended_ms: NOW - 20 * MINUTE }),
};

const liveRow = { ...run(12, "DEMO-12"), phase: "reviewing", round: 1, max_rounds: 3 };

/** `demo` live in run 12 with `actions` as given; `/runs/N` answers `runs`
 *  and every `POST /actions/...` goes to `post`. */
async function mount(id: number, { actions = true, post }: { actions?: boolean; post?: Fetch } = {}) {
  window.location.hash = `#/runs/demo/${id}`;
  const polls = fleet({ items: [], runs: [liveRow, run(13, "DEMO-13")] }, { items: [] });
  const fake = fakeDeps(async (url, init) => {
    if (init?.method === "POST" && post != null) return post(url, init);
    if (url === `${DEMO}/status`) return Response.json({ ...((await (await polls(url)).json()) as Status), actions });
    if (url === `${DEMO}/tickets/DEMO-40`) return Response.json({
      ...servedTicket,
      ticket: "DEMO-40",
      runs: [40, 39].map((id, index) => ({
        run: id, attempt: 2 - index, phase: "failed", outcome: "failed", rounds: 0,
        pr_url: null, started_ms: NOW - 30 * MINUTE, ended_ms: runs[id]!.run.ended_ms,
      })),
    });
    const found = /^\/runs\/(\d+)$/.exec(url.slice(DEMO.length))?.[1];
    if (url.startsWith(DEMO) && found != null) {
      const answer = runs[Number(found)];
      return answer ? Response.json(answer) : new Response("not found", { status: 404 });
    }
    return polls(url);
  });
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const actionsBox = () => document.querySelector<HTMLElement>("[data-run-actions]");
const labels = () => [...(actionsBox()?.querySelectorAll("button") ?? [])].map((button) => button.textContent);
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const dialog = () => screen.getByRole("dialog", { name: "Abort run 12?" });
const posted = (seen: { url: string; body: unknown }[]) => seen.map(({ url, body }) => ({ url, body }));

async function press(name: string, scope: HTMLElement = actionsBox()!) {
  await act(async () => void fireEvent.click(within(scope).getByRole("button", { name })));
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a live run's Pause posts the run with the page's note, reads Pausing… in flight and toasts the request", async () => {
  let release = () => {};
  const post = fakeFetch({ ok: true, detail: "pause requested for run 12" }, new Promise<void>((done) => (release = done)));
  await mount(12, { post: post.fetchImpl });
  expect(labels()).toEqual(["Pause", "Abort"]);
  await press("Pause");
  expect(posted(post.seen)).toEqual([{ url: `${DEMO}/actions/pause`, body: { run: 12, note: "from the Run page" } }]);
  const busy = within(actionsBox()!).getByRole("button", { name: "Pausing…" });
  expect(busy.getAttribute("aria-busy")).toBe("true");
  await act(async () => release());
  for (let round = 0; round < 4; round += 1) await act(settle);
  expect(toast()).toBe("Pause requested for run 12.");
});

test("Abort asks for a reason in a confirm that Esc closes unposted, then posts the reason without closing the PR", async () => {
  const post = fakeFetch({ ok: true, detail: "aborted run 12" });
  await mount(12, { post: post.fetchImpl });
  await press("Abort");
  expect(dialog().textContent).toContain(
    "Review round 2 stops now. The branch task/demo-12-x and PR #524 are kept, and DEMO-12 waits for you to requeue it.",
  );
  expect(document.activeElement?.textContent).toBe("Keep running");
  await act(async () => void fireEvent.keyDown(document, { key: "Escape" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(post.seen).toEqual([]);
  await press("Abort");
  const confirm = within(dialog()).getByRole("button", { name: "Abort run" });
  expect(confirm.hasAttribute("disabled")).toBe(true);
  await act(async () => void fireEvent.change(within(dialog()).getByRole("textbox", { name: "Reason · saved with the run" }), { target: { value: "wrong ticket" } }));
  expect(confirm.hasAttribute("disabled")).toBe(false);
  await press("Abort run", dialog());
  expect(posted(post.seen)).toEqual([{ url: `${DEMO}/actions/abort`, body: { run: 12, close: false, note: "wrong ticket" } }]);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(toast()).toBe("Aborted run 12.");
});

test("navigating to a cached run closes Abort and requires a fresh reason for that run", async () => {
  const post = fakeFetch({ ok: true, detail: "aborted run 13" });
  await mount(13, { post: post.fetchImpl });
  const navigate = async (id: number) => {
    await act(async () => {
      window.location.hash = `#/runs/demo/${id}`;
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    for (let round = 0; round < 6; round += 1) await act(settle);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(`DEMO-${id} title`);
  };
  await navigate(12);
  await press("Abort");
  await act(async () => void fireEvent.change(within(dialog()).getByRole("textbox"), { target: { value: "reason for twelve" } }));
  expect(within(dialog()).getByRole("button", { name: "Abort run" }).hasAttribute("disabled")).toBe(false);
  await navigate(13);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(post.seen).toEqual([]);
  await press("Abort");
  const nextDialog = screen.getByRole("dialog", { name: "Abort run 13?" });
  const reason = within(nextDialog).getByRole("textbox", { name: "Reason · saved with the run" });
  expect((reason as HTMLTextAreaElement).value).toBe("");
  expect(within(nextDialog).getByRole("button", { name: "Abort run" }).hasAttribute("disabled")).toBe(true);
  await act(async () => void fireEvent.change(reason, { target: { value: "reason for thirteen" } }));
  await press("Abort run", nextDialog);
  expect(posted(post.seen)).toEqual([{ url: `${DEMO}/actions/abort`, body: { run: 13, close: false, note: "reason for thirteen" } }]);
});

test("a failed run that is its chain's newest offers Requeue alone and shows a refusal under it; a merged run offers nothing", async () => {
  const post = fakeFetch({ ok: false, detail: "DEMO-40 has a live run" });
  await mount(40, { post: post.fetchImpl });
  expect(labels()).toEqual(["Requeue"]);
  await press("Requeue");
  expect(posted(post.seen)).toEqual([{ url: `${DEMO}/actions/requeue`, body: { ticket: "DEMO-40" } }]);
  expect(actionsBox()!.querySelector("[data-action-detail]")?.textContent).toBe("DEMO-40 has a live run");
  expect(toast()).toBe("");
  cleanup();
  await mount(9);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-9 title");
  expect(actionsBox()).toBeNull();
});

test("on a host with actions off, Pause and Abort are drawn disabled and say how to turn actions on", async () => {
  await mount(12, { actions: false });
  for (const name of ["Pause", "Abort"]) {
    const button = within(actionsBox()!).getByRole("button", { name });
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(button.getAttribute("title")).toBe("Actions are off on this host. [serve] actions = true turns them on.");
  }
});

test("a superseded failed attempt hides Requeue even when its truncated chain ends there and no attempt is live", async () => {
  await mount(39);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("DEMO-40 title");
  expect(actionsBox()).toBeNull();
  cleanup();
  await mount(40);
  expect(labels()).toEqual(["Requeue"]);
});
