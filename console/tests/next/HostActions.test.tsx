import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { HostStatus, Status } from "../../src/lib/contract.gen";
import { tokenedFetch, type Fetch } from "../../src/lib/poll";
import { storeToken, tokenKey } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, NO_ATTENTION, peersFetch, settle, type StubDaemon } from "../harness";

const ORIGIN = "http://writer-host:7710";
const ADDRESS = "writer-host:7710";
const SPARE = "http://spare:7710";
const NOW = new Date(2026, 9, 16, 16, 53).getTime();
const fixtures = new URL("../../../tests/fixtures/serve/", import.meta.url);
const host = await Bun.file(new URL("host-status.json", fixtures)).json() as HostStatus;
const project = await Bun.file(new URL("status.json", fixtures)).json() as Status;
const toast = () => document.querySelector("[data-toast]")!;
const card = (name = "writer-host") => screen.getByRole("article", { name });

async function flush() {
  for (let round = 0; round < 4; round += 1) await act(settle);
}

async function press(name: string, scope = card()) {
  await act(async () => void fireEvent.click(within(scope).getByRole("button", { name })));
  await flush();
}

function source({ actions = true, projectDaemon = false, post }: { actions?: boolean; projectDaemon?: boolean; post?: Fetch } = {}) {
  const daemons: Record<string, StubDaemon> = {
    [ORIGIN]: { status: projectDaemon ? { ...project, actions } : { ...host, projects: [], actions }, attention: NO_ATTENTION },
    [SPARE]: { status: { ...host, projects: [], actions: true }, attention: NO_ATTENTION },
  };
  const roots = peersFetch(ORIGIN, daemons);
  const statuses: string[] = [];
  const gates = new Map<string, Promise<void>>();
  const fetch: Fetch = async (url, init) => {
    if (init?.method === "POST" && post) return post(url, init);
    if (url.endsWith("/status")) statuses.push(url);
    await gates.get(new URL(url).origin);
    return roots(url, init);
  };
  return {
    fetch, statuses,
    fail: (base: string) => { daemons[base] = { down: new Error("connection refused") }; },
    hold: (base: string) => {
      let release = () => {};
      gates.set(base, new Promise<void>((done) => { release = done; }));
      return release;
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
    poll: async () => {
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

test("Run sweep posts once to the daemon root with its bearer, stays busy until answered and refreshes the fleet", async () => {
  storeToken(ADDRESS, "sweep-secret-8a71");
  let release = () => {};
  const post = fakeFetch({ ok: true, detail: "started" }, new Promise<void>((done) => { release = done; }));
  const view = await mount(source({ post: post.fetchImpl }));
  const before = view.statuses.length;
  await press("Run sweep");
  expect(post.seen).toEqual([{ url: `${ORIGIN}/actions/run-sweep`, method: "POST", authorization: "Bearer sweep-secret-8a71", body: {} }]);
  expect(within(card()).getByRole("button", { name: "Sweeping…" }).getAttribute("aria-busy")).toBe("true");
  expect(view.statuses).toHaveLength(before);
  await act(async () => release());
  await flush();
  expect(toast().textContent).toBe("Asked writer-host to sweep.");
  expect(within(card()).getByRole("button", { name: "Run sweep" }).hasAttribute("disabled")).toBe(false);
  expect(view.statuses).toHaveLength(before + 2);
});

test("a refused sweep shows the daemon's detail under the card head without rereading the fleet", async () => {
  const post = fakeFetch({ ok: false, detail: "systemctl exited 1" });
  const view = await mount(source({ post: post.fetchImpl }));
  const before = view.statuses.length;
  await press("Run sweep");
  expect(post.seen).toHaveLength(1);
  expect(within(card()).getByText("systemctl exited 1")).toBeTruthy();
  expect(toast().textContent).toBe("");
  expect(view.statuses).toHaveLength(before);
});

test("root actions off disables Run sweep with its explanation and project daemons have no sweep action", async () => {
  await mount(source({ actions: false }));
  const button = within(card()).getByRole("button", { name: "Run sweep" });
  expect(button.hasAttribute("disabled")).toBe(true);
  expect(button.getAttribute("title")).toBe(ACTIONS_OFF);
  cleanup();
  await mount(source({ projectDaemon: true }));
  expect(within(card()).queryByRole("button", { name: "Run sweep" })).toBeNull();
});

test("Retry now polls once and remains busy until a failing daemon answers, then reports the attempt time", async () => {
  const view = await mount();
  view.fail(SPARE);
  await view.poll();
  expect(within(card("spare")).getByRole("button", { name: "Run sweep" }).hasAttribute("disabled")).toBe(true);
  const release = view.hold(SPARE);
  const before = view.statuses.filter((url) => url === `${SPARE}/status`).length;
  await press("Retry now", card("spare"));
  expect(within(card("spare")).getByRole("button", { name: "Retrying…" }).getAttribute("aria-busy")).toBe("true");
  expect(view.statuses.filter((url) => url === `${SPARE}/status`)).toHaveLength(before + 1);
  await act(async () => release());
  await flush();
  expect(toast().textContent).toBe("spare still is not answering. Tried at 16:53.");
  expect(within(card("spare")).getByRole("button", { name: "Retry now" }).hasAttribute("disabled")).toBe(false);
});

test("the all-down banner retries both daemons in one fleet poll", async () => {
  const view = await mount();
  view.fail(ORIGIN);
  view.fail(SPARE);
  await view.poll();
  const before = view.statuses.length;
  await press("Try again", screen.getByText("No host is answering this console.").closest('[role="status"]') as HTMLElement);
  expect(view.statuses.slice(before).sort()).toEqual([`${ORIGIN}/status`, `${SPARE}/status`].sort());
});

test("a saved token is masked and accepted, Forget removes it and Undo restores the same browser token", async () => {
  const secret = "never-render-this-token-3f9a";
  storeToken(ADDRESS, secret);
  const view = await mount();
  expect(card().textContent).toContain("saved in this browser");
  expect(card().textContent).toContain("accepted");
  expect(card().textContent).toContain("••••");
  expect(document.body.innerHTML).not.toContain(secret);
  expect(document.body.innerHTML).not.toContain("3f9a");
  const before = view.statuses.length;
  await press("Forget");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
  expect(toast().textContent).toContain("Forgot the token for writer-host in this browser.");
  expect(within(card()).getByLabelText("Serve token for writer-host", { selector: "input" })).toBeTruthy();
  expect(view.statuses.length).toBeGreaterThan(before);
  await press("Undo", toast() as HTMLElement);
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe(secret);
  expect(card().textContent).toContain("saved in this browser");
});

test("Undo preserves the restored token when Forget's unauthenticated poll answers 401 after the authenticated poll", async () => {
  const secret = "restored-secret-8b21";
  storeToken(ADDRESS, secret);
  const input = source();
  let release = () => {};
  const pending = new Promise<void>((done) => { release = done; });
  let unauthenticated = 0;
  let authenticated = 0;
  await mount({ ...input, fetch: tokenedFetch(async (url, init) => {
    if (url.startsWith(ORIGIN)) {
      if (!new Headers(init?.headers).has("authorization")) {
        unauthenticated += 1;
        await pending;
        return new Response("Unauthorized", { status: 401 });
      }
      authenticated += 1;
    }
    return input.fetch(url, init);
  }) });
  await press("Forget");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
  expect(unauthenticated).toBe(3);
  const before = authenticated;
  await press("Undo", toast() as HTMLElement);
  expect(authenticated).toBe(before + 3);
  expect(card().textContent).toContain("accepted");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe(secret);
  await act(async () => release());
  await flush();
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe(secret);
  expect(card().textContent).toContain("saved in this browser");
});

test("Save token refuses header-unsafe input and saves a valid token with an empty field and a fresh poll", async () => {
  const view = await mount();
  const field = within(card()).getByLabelText("Serve token for writer-host", { selector: "input" }) as HTMLInputElement;
  expect(field.type).toBe("password");
  expect(field.autocomplete).toBe("off");
  expect(field.placeholder).toBe("Paste the serve token");
  expect(within(card()).getByRole("button", { name: "Save token" }).hasAttribute("disabled")).toBe(true);
  await act(async () => void fireEvent.change(field, { target: { value: "abc 123" } }));
  await press("Save token");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
  expect(within(card()).getByText("Token has a character the header cannot carry")).toBeTruthy();
  const before = view.statuses.length;
  await act(async () => void fireEvent.change(field, { target: { value: "abc123" } }));
  await press("Save token");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("abc123");
  expect(toast().textContent).toBe("Saved the token for writer-host in this browser.");
  expect(view.statuses.length).toBeGreaterThan(before);
  await press("Forget");
  expect((within(card()).getByLabelText("Serve token for writer-host", { selector: "input" }) as HTMLInputElement).value).toBe("");
});


test("a fleet poll rejecting a saved bearer shows the 401 refusal beside the replacement token field", async () => {
  const secret = "rejected-secret-7d2a";
  storeToken(ADDRESS, secret);
  const input = source();
  let rejected = false;
  const view = await mount({ ...input, fetch: tokenedFetch(async (url, init) => {
    if (rejected && url.startsWith(ORIGIN)) return new Response("Unauthorized", { status: 401 });
    return input.fetch(url, init);
  }) });
  rejected = true;
  await view.poll();
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
  expect(card().textContent).toContain("refused: the daemon answered 401");
  expect(within(card()).getByLabelText("Serve token for writer-host", { selector: "input" })).toBeTruthy();
  expect(document.body.innerHTML).not.toContain(secret);
  rejected = false;
  await view.poll();
  expect(card().textContent).not.toContain("refused: the daemon answered 401");
});
