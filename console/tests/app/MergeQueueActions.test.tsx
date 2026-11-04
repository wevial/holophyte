import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import type { Attention, MergeQueue, MergeQueueEntry, Status } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { forgetToken, storeToken } from "../../src/lib/token";
import { fakeDeps, peersFetch, settle } from "../harness";

const RELOS = "http://writer-host:7710";
const TOKEN = "tok-queue";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const served = (await Bun.file(new URL("merge-queue.json", SERVE)).json()) as MergeQueue;

const happyDOM = (window as unknown as { happyDOM: { setViewport: (viewport: { width: number; height?: number }) => void } }).happyDOM;

const MINUTE = 60_000;
const NOW = Date.UTC(2026, 10, 3, 15, 52);
const at = (hour: number, minute: number) => Date.UTC(2026, 10, 3, hour, minute);

const pull = (number: number, title: string) => ({ number, title, author: "wevial", url: `https://github.com/example/relos/pull/${number}`, head_oid: null });

const entry = (position: number, number: number, title: string, rest: Partial<MergeQueueEntry> = {}): MergeQueueEntry => ({
  position, state: "QUEUED", enqueued_ms: at(15, 40 + position), estimated_s: position * 600, jump: false, solo: false,
  pr: pull(number, title), group_sha: null, building_ms: null, run: null,
  checks: { done: 0, total: 0, failed: 0, runs: [] }, ...rest,
});

const QUEUE: MergeQueue = {
  ...served,
  now: NOW,
  entries: [
    entry(1, 2547, "[Dependency Update]: Update Vite to 8.3.5", { state: "AWAITING_CHECKS", group_sha: "3f9c2ab0d1e2f3a4", building_ms: NOW - 3 * MINUTE }),
    entry(2, 2549, "[MCP] Add the unit lookup read tool", { run: { id: 231, ticket: "RELOS-231", phase: "merging", round: 0 } }),
    entry(3, 2550, "[PDF] Keep the page position when a PDF reloads", { run: { id: 229, ticket: "RELOS-229", phase: "merging", round: 0 } }),
    entry(4, 2552, "[Auth] Stop the sign-in redirect loop on an expired invite"),
  ],
  left: [],
};

interface Seen {
  route: string;
  body: unknown;
  authorization: string | null;
}

/** The relos daemon: its status with `actions`, the queue, and each action
 *  post recorded and answered with the next of `answers`. */
function daemon(actions: boolean, answers: object[] = []) {
  const posts: Seen[] = [];
  let reads = 0;
  const polls = peersFetch(RELOS, {
    [RELOS]: { status: { ...working, host: "writer-host", project: "/srv/dev/relos", actions }, attention: { level: "none", now: NOW, project: "/srv/dev/relos", items: [] } as Attention },
  });
  const fetch: Fetch = async (url, init) => {
    if (url === `${RELOS}/merge-queue`) {
      reads += 1;
      return Response.json(QUEUE);
    }
    if (url.startsWith(`${RELOS}/actions/`) && init?.method === "POST") {
      posts.push({ route: url.slice(RELOS.length), body: JSON.parse(String(init.body)), authorization: new Headers(init.headers).get("authorization") });
      return Response.json(answers.shift() ?? { ok: true, detail: "done" });
    }
    return polls(url, init);
  };
  return { fetch, posts, reads: () => reads };
}

async function mount(fetch: Fetch) {
  happyDOM.setViewport({ width: 1440, height: 900 });
  window.location.hash = "#/merge-queue?project=relos";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={RELOS} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  await flush();
}

const flush = async () => {
  for (let round = 0; round < 6; round += 1) await act(settle);
};

beforeEach(() => storeToken(addressOf(RELOS), TOKEN));

afterEach(() => {
  cleanup();
  forgetToken(addressOf(RELOS));
  window.location.hash = "";
  happyDOM.setViewport({ width: 1024, height: 768 });
});

const rows = () => [...screen.getByRole("list", { name: "relos merge queue" }).querySelectorAll<HTMLElement>("[data-entry]")];
const jumpIn = (row: HTMLElement) => within(row).queryByRole("button", { name: /Jump to front/ });
const dialog = () => screen.queryByRole("dialog");
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
const press = async (button: HTMLElement) => {
  await act(async () => void fireEvent.click(button));
  await flush();
};

async function openJump(on: ReturnType<typeof daemon>) {
  await mount(on.fetch);
  await press(jumpIn(rows()[3]!)!);
  return dialog()!;
}

test("Jump to front is on every row from position 2 down when the daemon allows actions, and on none when it does not", async () => {
  await mount(daemon(true).fetch);
  expect(rows().map((row) => jumpIn(row) != null)).toEqual([false, true, true, true]);
  cleanup();

  await mount(daemon(false).fetch);
  expect(rows()).toHaveLength(4);
  expect(rows().map((row) => jumpIn(row) != null)).toEqual([false, false, false, false]);
});

test("the jump dialog names the builds it restarts, position 1's dropped checks and the bypass it needs, and Cancel posts nothing", async () => {
  const on = daemon(true);
  const box = await openJump(on);
  expect(within(box).getByRole("heading").textContent).toBe("Jump #2552 to the front?");
  expect(box.textContent).toContain("Jump · relos");
  expect(box.textContent).toContain("[Auth] Stop the sign-in redirect loop on an expired invite");
  expect([...box.querySelectorAll("[data-jump-warning]")].map((line) => line.textContent)).toEqual([
    "restarts the builds of #2547, #2549 and #2550; each merges about 10m later",
    "#2547 drops 3m of checks already run",
    "needs bypass permission on the repository's merge rules",
  ]);
  expect(box.textContent).toContain("#2552 builds next and merges in about 10m.");

  await press(within(box).getByRole("button", { name: "Cancel" }));
  expect(dialog()).toBeNull();
  expect(on.posts).toEqual([]);
});

test("a taken jump posts the pull request once with the bearer, closes the dialog, toasts and reads the queue again", async () => {
  const on = daemon(true, [{ ok: true, detail: "jumped" }]);
  const box = await openJump(on);
  const before = on.reads();
  await press(within(box).getByRole("button", { name: "Jump to front" }));
  expect(on.posts).toEqual([{ route: "/actions/queue-jump", body: { pr: 2552 }, authorization: `Bearer ${TOKEN}` }]);
  expect(dialog()).toBeNull();
  expect(toast()).toBe("#2552 jumped to the front");
  expect(on.reads()).toBeGreaterThan(before);
});

test("a refused jump keeps the dialog open with GitHub's detail under its buttons, and can be pressed again", async () => {
  const on = daemon(true, [{ ok: false, reason: "refused", detail: "Repository rule violations found" }, { ok: true, detail: "jumped" }]);
  const box = await openJump(on);
  await press(within(box).getByRole("button", { name: "Jump to front" }));
  expect(dialog()).not.toBeNull();
  expect(within(box).getByRole("alert").textContent).toBe("Repository rule violations found");

  await press(within(box).getByRole("button", { name: "Jump to front" }));
  expect(on.posts.map((post) => post.route)).toEqual(["/actions/queue-jump", "/actions/queue-jump"]);
  expect(dialog()).toBeNull();
});

test("a hand pull request's open row offers Remove from queue beside Open on GitHub; a factory one's offers only Open on GitHub", async () => {
  const on = daemon(true, [{ ok: true, detail: "removed" }]);
  await mount(on.fetch);
  const [first, second] = rows();
  await press(within(first!).getByRole("button", { name: /^Position 1/ }));
  const detail = first!.querySelector<HTMLElement>("[data-entry-detail]")!;
  expect(within(detail).getByRole("link", { name: "Open on GitHub" }).getAttribute("href")).toBe("https://github.com/example/relos/pull/2547");
  await press(within(detail).getByRole("button", { name: "Remove from queue" }));
  expect(on.posts).toEqual([{ route: "/actions/queue-remove", body: { pr: 2547 }, authorization: `Bearer ${TOKEN}` }]);
  expect(toast()).toBe("#2547 removed from the queue");

  await press(within(second!).getByRole("button", { name: /^Position 2/ }));
  const factory = second!.querySelector<HTMLElement>("[data-entry-detail]")!;
  expect(within(factory).getByRole("link", { name: "Open on GitHub" })).toBeDefined();
  expect(within(factory).queryByRole("button", { name: "Remove from queue" })).toBeNull();
});
