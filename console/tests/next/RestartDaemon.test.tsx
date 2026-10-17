import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { HostStatus, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, NO_ATTENTION, peersFetch, settle, type StubDaemon } from "../harness";

const ORIGIN = "http://writer-host:7710";
const NOW = 100_000;
const fixtures = new URL("../../../tests/fixtures/serve/", import.meta.url);
const host = await Bun.file(new URL("host-status.json", fixtures)).json() as HostStatus;
const project = await Bun.file(new URL("status.json", fixtures)).json() as Status;
const card = () => screen.getByRole("article", { name: "writer-host" });
const toast = () => document.querySelector("[data-toast]")!;
const restarting = "Restarting · back in about 20s";

async function flush() {
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function press(name: string, scope = card()) {
  await act(async () => void fireEvent.click(within(scope).getByRole("button", { name })));
  await flush();
}

function source({ actions = true, projectDaemon = false, post }: { actions?: boolean; projectDaemon?: boolean; post?: Fetch } = {}) {
  const root: HostStatus = {
    ...host, actions, now: NOW, daemon: { ...host.daemon, started_ms: 1000 },
    build: { ...host.build, head: "0a1b2c3def456" },
    projects: host.projects.map((entry, index) => ({ ...entry, host: "writer-host", runs: index === 0 ? [
      { ...entry.runs[0]!, id: 1 }, { ...entry.runs[0]!, id: 2 },
    ] : [{ ...entry.runs[0]!, id: 3 }] })),
  };
  const daemons: Record<string, StubDaemon> = {
    [ORIGIN]: { status: projectDaemon ? { ...project, host: "writer-host", actions } : root, attention: NO_ATTENTION },
  };
  const roots = peersFetch(ORIGIN, daemons);
  const fetch: Fetch = async (url, init) => {
    if (init?.method === "POST" && post) return post(url, init);
    const match = /^\/projects\/(alpha|beta)\/(status|attention)$/.exec(url.slice(ORIGIN.length));
    if (match) return Response.json(match[2] === "attention" ? NO_ATTENTION : { ...project, host: "writer-host", project: `/root/${match[1]}` });
    return roots(url, init);
  };
  return {
    fetch, root,
    fail: () => { daemons[ORIGIN] = { down: new Error("connection refused") }; },
    answer: (started: number) => {
      root.daemon.started_ms = started;
      daemons[ORIGIN] = { status: root, attention: NO_ATTENTION };
    },
  };
}

async function mount(input = source()) {
  window.location.hash = "#/settings/hosts";
  const fake = fakeDeps(input.fetch);
  fake.clock.now = NOW;
  const client = consoleQueryClient();
  render(<QueryClientProvider client={client}><NextConsole base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await flush();
  return {
    ...input,
    poll: async (now = NOW) => {
      fake.clock.now = now;
      await act(async () => { await client.refetchQueries({ queryKey: ["fleet", ORIGIN] }); });
      await flush();
    },
  };
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("restart confirmation explains the checkout and all root runs, focuses Keep running, and Escape cancels without posting", async () => {
  const post = fakeFetch({ ok: true });
  await mount(source({ post: post.fetchImpl }));
  await press("Restart daemon");
  const dialog = screen.getByRole("dialog", { name: "Restart writer-host" });
  expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Keep running" }));
  expect(within(dialog).getByRole("heading", { name: "Restart the daemon on writer-host?" })).toBeTruthy();
  expect(dialog.textContent).toContain("The daemon finishes the requests it is answering, stops, and starts again on the checkout's build 0a1b2c3.");
  expect(dialog.textContent).toContain("3 runs in flight keep going: they run in the loop, not in this daemon");
  expect(dialog.textContent).toContain("the supervisor and the sweep are not touched");
  expect(dialog.textContent).toContain("this console loses writer-host for up to 20s");
  await act(async () => void fireEvent.keyDown(document, { key: "Escape" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(post.seen).toHaveLength(0);
});

test("restart posts once with the bearer, remains restarting through failed and old root polls, and reports a newer daemon", async () => {
  storeToken("writer-host:7710", "restart-secret-8a71");
  let release = () => {};
  const post = fakeFetch({ ok: true }, new Promise<void>((done) => { release = done; }));
  const view = await mount(source({ post: post.fetchImpl }));
  await press("Restart daemon");
  await press("Restart daemon", screen.getByRole("dialog"));
  expect(post.seen).toEqual([{ url: `${ORIGIN}/actions/restart-daemon`, method: "POST", authorization: "Bearer restart-secret-8a71", body: {} }]);
  expect(within(screen.getByRole("dialog")).getByRole("button", { name: "Restarting…" }).getAttribute("aria-busy")).toBe("true");
  await act(async () => release());
  await flush();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(within(card()).getByText(restarting)).toBeTruthy();
  expect(within(card()).getByRole("status").textContent).toBe("Restarting the daemon. Runs keep going; this card comes back when it answers.");
  for (const name of ["Run sweep", "Restart daemon"]) expect(within(card()).getByRole("button", { name }).hasAttribute("disabled")).toBe(true);
  expect(toast().textContent).not.toContain("Restarted the daemon");
  view.fail();
  await view.poll();
  expect(within(card()).getByText(restarting)).toBeTruthy();
  expect(card().textContent).not.toContain("Not answering");
  view.answer(1000);
  await view.poll();
  expect(within(card()).getByText(restarting)).toBeTruthy();
  view.answer(2000);
  await view.poll();
  expect(toast().textContent).toBe("Restarted the daemon on writer-host.");
  expect(within(card()).getByText("Healthy")).toBeTruthy();
  expect(within(card()).queryByText(restarting)).toBeNull();
  for (const name of ["Run sweep", "Restart daemon"]) expect(within(card()).getByRole("button", { name }).hasAttribute("disabled")).toBe(false);
});

test("a refused restart keeps its ledger detail in the dialog without entering restart state", async () => {
  const detail = "could not record in the ledger; nothing run";
  const post = fakeFetch({ ok: false, detail });
  await mount(source({ post: post.fetchImpl }));
  await press("Restart daemon");
  await press("Restart daemon", screen.getByRole("dialog"));
  expect(post.seen).toHaveLength(1);
  expect(within(screen.getByRole("dialog", { name: "Restart writer-host" })).getByText(detail)).toBeTruthy();
  expect(within(card()).queryByText(restarting)).toBeNull();
  expect(within(card()).getByText("Healthy")).toBeTruthy();
});

test("root actions off explains the disabled restart, a failed root disables it, and a project daemon has no restart", async () => {
  await mount(source({ actions: false }));
  const button = within(card()).getByRole("button", { name: "Restart daemon" });
  expect(button.hasAttribute("disabled")).toBe(true);
  expect(button.getAttribute("title")).toBe(ACTIONS_OFF);
  cleanup();
  const view = await mount();
  view.fail();
  await view.poll();
  expect(within(card()).getByRole("button", { name: "Restart daemon" }).hasAttribute("disabled")).toBe(true);
  cleanup();
  await mount(source({ projectDaemon: true }));
  expect(within(card()).queryByRole("button", { name: "Restart daemon" })).toBeNull();
});

test("after sixty seconds without a new daemon the card returns to its last failed poll without a success toast", async () => {
  const post = fakeFetch({ ok: true });
  const view = await mount(source({ post: post.fetchImpl }));
  await press("Restart daemon");
  await press("Restart daemon", screen.getByRole("dialog"));
  view.fail();
  await view.poll(NOW + 59_000);
  expect(within(card()).getByText(restarting)).toBeTruthy();
  await view.poll(NOW + 60_000);
  expect(within(card()).queryByText(restarting)).toBeNull();
  expect(within(card()).getByText(/Not answering/)).toBeTruthy();
  expect(toast().textContent).not.toContain("Restarted the daemon");
});
