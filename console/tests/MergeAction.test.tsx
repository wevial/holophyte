import { afterEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { PullRequestTable } from "../src/components/PullRequestTable";
import { tokenedFetch } from "../src/lib/poll";
import { storeToken } from "../src/lib/token";
import type { AttentionItem, Status } from "../src/lib/types";
import ready from "../../tests/fixtures/serve/run-merge-ready.json";
import { fakeFetch } from "./actionFakes";
import { fixture, hostOf, settle } from "./harness";

const status = await fixture<Status>("idle.json");
const item: AttentionItem = {
  kind: "pr_open", level: "attention", run: 47, ticket: "KO-7",
  ticket_url: "https://linear.app/team/issue/KO-7", pr_url: "https://github.com/o/r/pull/2170",
  reason: "Waiting for maintainer review", asked_ms: status.now - 600000,
  pr: { number: 2170, checks: "success", review: "approved", threads: 0 },
};
const attention = { level: "attention" as const, now: status.now, items: [item] };
const host = hostOf({ ...status, project: "/projects/repo", actions: true }, attention);
const readiness = { ...ready, run: 47 };
afterEach(() => { cleanup(); localStorage.clear(); });

/** The table over `hosts` at poll count `polls`, reading readiness from
 *  `answer` and posting merges to `post`, both carrying the stored bearer. */
async function show(answer: Record<string, unknown>, { post = fakeFetch({ ok: true, detail: "done" }), hosts = [host] } = {}) {
  storeToken(host.address, "test-token");
  const read = fakeFetch(answer);
  const deps = { fetch: tokenedFetch(read.fetchImpl) };
  const table = (polls: number) => <PullRequestTable hosts={hosts} project="all" now={status.now}
    polls={polls} deps={deps} actionFetch={post.fetchImpl} />;
  const view = render(table(0));
  await act(settle);
  const cell = () => within(screen.getAllByRole("row")[1]!).getAllByRole("cell")[4]!;
  return { read: read.seen, post: post.seen, cell, poll: (polls: number) => view.rerender(table(polls)) };
}

test("a ready parked pull request shows Merge, read from its daemon's readiness with the bearer", async () => {
  const { read, cell } = await show(readiness);
  expect(within(cell()).getByRole("button", { name: "Merge" })).toBeTruthy();
  expect(read).toEqual([{ url: `${host.base}/runs/47/merge`, method: undefined,
    authorization: "Bearer test-token", body: undefined }]);
});

test("a pull request not yet ready shows one line saying why in place of Merge", async () => {
  const lines = {
    review_not_approved: "waiting on review approval",
    checks_pending: "checks running",
    checks_failing: "checks failing",
    conflicting: "merge conflict with main",
    mergeable_unknown: "GitHub is still checking mergeability",
    threads_unresolved: "review threads unresolved",
    head_moved: "branch moved since approval",
    github_unreadable: "GitHub could not be read",
    a_newer_reason: "the daemon's own words for a newer reason",
  };
  for (const [reason, line] of Object.entries(lines)) {
    const { cell } = await show({ ...readiness, ready: false, reason, detail: "the daemon's own words for a newer reason" });
    expect(within(cell()).queryByRole("button", { name: "Merge" })).toBeNull();
    expect(Array.from(cell().querySelectorAll("[data-merge-waiting]"), node => node.textContent)).toEqual([line]);
    cleanup();
  }
});

test("a run that is not a human-approval park, or a daemon without actions, draws no merge control", async () => {
  for (const reason of ["not_human_approval", "not_parked"]) {
    const { cell } = await show({ ...readiness, ready: false, reason, detail: "not a human-approval park" });
    expect(within(cell()).queryByRole("button", { name: "Merge" })).toBeNull();
    expect(cell().querySelector("[data-merge-waiting]")).toBeNull();
    expect(within(cell()).getByRole("button", { name: "Open PR" })).toBeTruthy();
    cleanup();
  }
  const quiet = hostOf({ ...status, project: "/projects/repo" }, attention);
  const { read, cell } = await show(readiness, { hosts: [quiet] });
  expect(read).toEqual([]);
  expect(within(cell()).queryByRole("button", { name: "Merge" })).toBeNull();
  expect(within(cell()).getByRole("button", { name: "Open PR" })).toBeTruthy();
});

test("Merge opens an in-row confirm and Cancel closes it, posting nothing and opening no browser dialog", async () => {
  const dialogs = [spyOn(window, "confirm"), spyOn(window, "alert"), spyOn(window, "prompt")];
  try {
    const { post, cell } = await show(readiness);
    await act(async () => { fireEvent.click(within(cell()).getByRole("button", { name: "Merge" })); });
    const group = within(cell()).getByRole("group");
    expect(group.textContent).toContain("Merge PR #2170 into main?");
    expect(within(group).getByRole("button", { name: "Confirm merge" })).toBeTruthy();
    fireEvent.click(within(group).getByRole("button", { name: "Cancel" }));
    expect(within(cell()).queryByRole("group")).toBeNull();
    expect(within(cell()).getByRole("button", { name: "Merge" })).toBeTruthy();
    await act(settle);
    expect(post).toEqual([]);
    for (const dialog of dialogs) expect(dialog).not.toHaveBeenCalled();
  } finally { for (const dialog of dialogs) dialog.mockRestore(); }
});

/** `show` with readiness ready and the confirm group open. */
async function confirmWith(answer: Record<string, unknown> | (() => Response)) {
  const shown = await show(readiness, { post: fakeFetch(answer) });
  await act(async () => { fireEvent.click(within(shown.cell()).getByRole("button", { name: "Merge" })); });
  await act(async () => { fireEvent.click(within(shown.cell()).getByRole("button", { name: "Confirm merge" })); await settle(); });
  return shown;
}

test("Confirm merge posts the run once with the bearer, shows the daemon's detail and hides Merge", async () => {
  const { post, cell } = await confirmWith({ ok: true, detail: "KO-7 approved: run 47 released" });
  expect(post).toEqual([{ url: `${host.base}/actions/merge`, method: "POST", authorization: "Bearer test-token", body: { run: 47 } }]);
  const line = within(cell()).getByRole("status");
  expect(line.textContent).toBe("KO-7 approved: run 47 released");
  expect(line.getAttribute("data-ok")).toBe("true");
  expect(within(cell()).queryByRole("button", { name: "Merge" })).toBeNull();
});

test("while a confirmed merge is in flight it cannot be cancelled or posted again", async () => {
  let release!: () => void;
  const gate = new Promise<void>(done => { release = done; });
  const { post, cell } = await show(readiness, { post: fakeFetch({ ok: true, detail: "released" }, gate) });
  await act(async () => { fireEvent.click(within(cell()).getByRole("button", { name: "Merge" })); });
  await act(async () => { fireEvent.click(within(cell()).getByRole("button", { name: "Confirm merge" })); await settle(); });
  const cancel = within(cell()).getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
  expect(cancel.disabled).toBe(true);
  await act(async () => { fireEvent.click(cancel); });
  expect(within(cell()).queryByRole("button", { name: "Merge" })).toBeNull();
  await act(async () => { release(); await settle(); });
  expect(post).toHaveLength(1);
  expect(within(cell()).getByRole("status").textContent).toBe("released");
});

test("a refused or failed merge shows its detail as a refusal", async () => {
  const refused = await confirmWith({ ok: false, reason: "head_moved", detail: "the branch head moved" });
  expect(within(refused.cell()).getByRole("status").textContent).toBe("the branch head moved");
  expect(within(refused.cell()).getByRole("status").getAttribute("data-ok")).toBe("false");
  cleanup();
  const missing = await confirmWith(() => new Response("not found", { status: 404 }));
  expect(within(missing.cell()).getByRole("status").textContent).toBe(`${host.base}/actions/merge answered 404`);
  expect(within(missing.cell()).getByRole("status").getAttribute("data-ok")).toBe("false");
});

test("readiness is read once across six polls and again at the sixth", async () => {
  const { read, poll } = await show(readiness);
  for (const polls of [1, 2, 3, 4, 5]) { poll(polls); await act(settle); }
  expect(read).toHaveLength(1);
  poll(6);
  await act(settle);
  expect(read.map(request => request.url)).toEqual([`${host.base}/runs/47/merge`, `${host.base}/runs/47/merge`]);
});
