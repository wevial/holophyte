import { afterAll, afterEach, beforeAll, beforeEach, expect, jest, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { HostStatus, Status } from "../../src/lib/contract.gen";
import { tokenedFetch, type Fetch } from "../../src/lib/poll";
import { storeToken, tokenKey } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, NO_ATTENTION, peersFetch, settle, type StubDaemon } from "../harness";

const ORIGIN = "http://writer-host:7710";
const ADDRESS = "writer-host:7710";
const SPARE = "http://spare:7710";
const NOW = new Date(2026, 9, 16, 16, 53).getTime();
const fixtures = new URL("../../../tests/fixtures/serve/", import.meta.url);
const host = await Bun.file(new URL("host-status.json", fixtures)).json() as HostStatus;
const project = await Bun.file(new URL("status.json", fixtures)).json() as Status;
const toasts = () => [...document.querySelectorAll<HTMLElement>("[data-toast]")];
const toast = () => toasts()[0]?.textContent ?? "";
const savedToast = "Saved the token for writer-host in this browser.";
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
  render(<QueryClientProvider client={client}><ConsoleApp base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await flush();
  return {
    ...input, client,
    poll: async () => {
      await act(async () => { await client.refetchQueries({ queryKey: ["fleet", ORIGIN] }); });
      await flush();
    },
  };
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/`));
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
  expect(toast()).toBe("Asked writer-host to sweep.");
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
  expect(toasts()).toEqual([]);
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
  const opening = card("spare").firstElementChild!;
  expect(opening.getAttribute("role")).toBe("status");
  expect(opening.textContent).toContain("spare has not answered since");
  expect(within(card("spare")).getByRole("button", { name: "Retry now" })).toBeTruthy();
  expect(within(card("spare")).getByRole("button", { name: "Run sweep" }).hasAttribute("disabled")).toBe(true);
  expect(within(card("spare")).getByRole("button", { name: "Restart daemon" }).hasAttribute("disabled")).toBe(true);
  const release = view.hold(SPARE);
  const before = view.statuses.filter((url) => url === `${SPARE}/status`).length;
  await press("Retry now", card("spare"));
  expect(within(card("spare")).getByRole("button", { name: "Retrying…" }).getAttribute("aria-busy")).toBe("true");
  expect(view.statuses.filter((url) => url === `${SPARE}/status`)).toHaveLength(before + 1);
  await act(async () => release());
  await flush();
  expect(toast()).toBe("spare still is not answering. Tried at 16:53.");
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
  expect(within(card()).getByRole("button", { name: "Forget" })).toBeTruthy();
  expect(document.body.innerHTML).not.toContain(secret);
  expect(document.body.innerHTML).not.toContain("3f9a");
  const before = view.statuses.length;
  await press("Forget");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
  expect(toast()).toContain("Forgot the token for writer-host in this browser.");
  expect(within(card()).getByLabelText("Serve token for writer-host", { selector: "input" })).toBeTruthy();
  expect(view.statuses.length).toBeGreaterThan(before);
  await press("Undo", toasts()[0]!);
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe(secret);
  expect(card().textContent).toContain("saved in this browser");
  expect(toasts()).toEqual([]);
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
  await press("Undo", toasts()[0]!);
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
  expect(within(card()).getByRole("button", { name: "Save token" }).hasAttribute("disabled")).toBe(false);
  await press("Save token");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
  expect(within(card()).getByText("Token has a character the header cannot carry")).toBeTruthy();
  const before = view.statuses.length;
  await act(async () => void fireEvent.change(field, { target: { value: "abc123" } }));
  await press("Save token");
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("abc123");
  expect(toast()).toBe(savedToast);
  await waitFor(() =>
    expect(screen.getAllByRole("status").find((region) => region.getAttribute("aria-live") === "polite")?.textContent).toBe(savedToast),
  );
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


test("a saved replacement survives an older authenticated poll answering 401", async () => {
  storeToken(ADDRESS, "old-token");
  const input = source();
  let rejectOld = false;
  let release = () => {};
  const pending = new Promise<void>((done) => { release = done; });
  const held: string[] = [];
  const accepted: string[] = [];
  const view = await mount({ ...input, fetch: tokenedFetch(async (url, init) => {
    const bearer = new Headers(init?.headers).get("authorization");
    if (url.startsWith(ORIGIN) && rejectOld && bearer === "Bearer old-token") {
      held.push(url);
      await pending;
      return new Response("Unauthorized", { status: 401 });
    }
    if (url.startsWith(ORIGIN) && bearer === "Bearer replacement-token") accepted.push(url);
    return input.fetch(url, init);
  }) });
  rejectOld = true;
  await act(async () => { void view.client.refetchQueries({ queryKey: ["fleet", ORIGIN] }); });
  await flush();
  expect(held.sort()).toEqual([`${ORIGIN}/attention`, `${ORIGIN}/peers`, `${ORIGIN}/status`]);
  await press("Forget");
  const field = within(card()).getByLabelText("Serve token for writer-host", { selector: "input" });
  await act(async () => void fireEvent.change(field, { target: { value: "replacement-token" } }));
  await press("Save token");
  expect(accepted.sort()).toEqual([`${ORIGIN}/attention`, `${ORIGIN}/peers`, `${ORIGIN}/status`]);
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("replacement-token");
  expect(card().textContent).toContain("accepted");
  await act(async () => release());
  await flush();
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("replacement-token");
  expect(card().textContent).toContain("saved in this browser");
  expect(card().textContent).toContain("accepted");
});

test("Save token then Forget stacks two toasts newest first, and Undo restores the token and dismisses the Forget toast", async () => {
  await mount();
  const field = within(card()).getByLabelText("Serve token for writer-host", { selector: "input" });
  await act(async () => void fireEvent.change(field, { target: { value: "abc123" } }));
  await press("Save token");
  await press("Forget");
  const shown = toasts();
  expect(shown).toHaveLength(2);
  expect(within(shown[0]!).getByText("Forgot the token for writer-host in this browser.")).toBeTruthy();
  expect(shown[1]!.textContent).toBe(savedToast);
  await press("Undo", shown[0]!);
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("abc123");
  expect(toasts().map((el) => el.textContent)).toEqual([savedToast]);
});

test("saving a replacement token removes Undo from an older Forget toast even when the stack expands or the Save toast closes", async () => {
  storeToken(ADDRESS, "old-token");
  await mount();
  await press("Forget");
  expect(within(toasts()[0]!).getByRole("button", { name: "Undo" })).toBeTruthy();
  const field = within(card()).getByLabelText("Serve token for writer-host", { selector: "input" });
  await act(async () => void fireEvent.change(field, { target: { value: "replacement-token" } }));
  await press("Save token");
  const region = document.querySelector<HTMLElement>('[data-slot="toast-region"]')!;
  await act(async () => void fireEvent.pointerEnter(region));
  await flush();
  expect(region.hasAttribute("data-expanded")).toBe(true);
  const shown = toasts();
  expect(shown).toHaveLength(2);
  expect(shown[0]!.textContent).toBe(savedToast);
  expect(shown[1]!.textContent).toBe("Forgot the token for writer-host in this browser.");
  expect(within(shown[1]!).queryByRole("button", { name: "Undo" })).toBeNull();
  await act(async () => void fireEvent.keyDown(shown[0]!, { key: "Escape" }));
  await flush();
  expect(toasts().map((el) => el.textContent)).toEqual(["Forgot the token for writer-host in this browser."]);
  expect(within(toasts()[0]!).queryByRole("button", { name: "Undo" })).toBeNull();
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("replacement-token");
});

test("forgetting tokens for two hosts keeps Undo working on both toasts when expanded", async () => {
  storeToken(ADDRESS, "writer-secret");
  storeToken("spare:7710", "spare-secret");
  await mount();
  await press("Forget", card("writer-host"));
  await press("Forget", card("spare"));
  const region = document.querySelector<HTMLElement>('[data-slot="toast-region"]')!;
  await act(async () => void fireEvent.pointerEnter(region));
  await flush();
  const shown = toasts();
  expect(shown).toHaveLength(2);
  expect(within(shown[0]!).getByText("Forgot the token for spare in this browser.")).toBeTruthy();
  expect(within(shown[1]!).getByText("Forgot the token for writer-host in this browser.")).toBeTruthy();
  await press("Undo", shown[1]!);
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("writer-secret");
  expect(localStorage.getItem(tokenKey("spare:7710"))).toBeNull();
  expect(toasts()).toHaveLength(1);
  await press("Undo", toasts()[0]!);
  expect(localStorage.getItem(tokenKey("spare:7710"))).toBe("spare-secret");
  expect(toasts()).toEqual([]);
});

test("keyboard activation of Save token announces the toast politely and keeps focus on the pressed control", async () => {
  const user = userEvent.setup();
  await mount();
  const field = within(card()).getByLabelText("Serve token for writer-host", { selector: "input" });
  await act(async () => void fireEvent.change(field, { target: { value: "abc123" } }));
  const save = within(card()).getByRole("button", { name: "Save token" });
  save.focus();
  expect(document.activeElement).toBe(save);
  await user.keyboard("{Enter}");
  await flush();
  expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("abc123");
  expect(toast()).toBe(savedToast);
  await waitFor(() =>
    expect(screen.getAllByRole("status").find((region) => region.getAttribute("aria-live") === "polite")?.textContent).toBe(savedToast),
  );
  expect(save.isConnected).toBe(true);
  expect(document.activeElement).toBe(save);
});

test("Undo on a paused Forget toast does not overwrite a replacement token saved in another tab", async () => {
  storeToken(ADDRESS, "old-token");
  await mount();
  jest.useFakeTimers();
  try {
    await act(async () => void fireEvent.click(within(card()).getByRole("button", { name: "Forget" })));
    expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
    expect(toasts()).toHaveLength(1);
    await act(async () => {
      window.dispatchEvent(new Event("blur"));
      jest.advanceTimersByTime(10_000);
    });
    expect(toasts()).toHaveLength(1);
    storeToken(ADDRESS, "other-tab-token");
    await act(async () => void window.dispatchEvent(new Event("focus")));
    await act(async () => void fireEvent.click(within(toasts()[0]!).getByRole("button", { name: "Undo" })));
    expect(localStorage.getItem(tokenKey(ADDRESS))).toBe("other-tab-token");
    expect(toasts()).toEqual([]);
  } finally {
    cleanup();
    jest.advanceTimersByTime(0);
    jest.useRealTimers();
  }
});

test("Undo on a paused Forget toast does not restore a superseded token after another tab saves and forgets a replacement", async () => {
  storeToken(ADDRESS, "original-token");
  await mount();
  jest.useFakeTimers();
  try {
    await act(async () => void fireEvent.click(within(card()).getByRole("button", { name: "Forget" })));
    expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
    expect(toasts()).toHaveLength(1);
    await act(async () => {
      window.dispatchEvent(new Event("blur"));
      jest.advanceTimersByTime(10_000);
    });
    expect(toasts()).toHaveLength(1);
    localStorage.setItem(tokenKey(ADDRESS), "replacement-token");
    window.dispatchEvent(new StorageEvent("storage", { key: tokenKey(ADDRESS), oldValue: null, newValue: "replacement-token", storageArea: localStorage }));
    localStorage.removeItem(tokenKey(ADDRESS));
    window.dispatchEvent(new StorageEvent("storage", { key: tokenKey(ADDRESS), oldValue: "replacement-token", newValue: null, storageArea: localStorage }));
    await act(async () => void window.dispatchEvent(new Event("focus")));
    await act(async () => void fireEvent.click(within(toasts()[0]!).getByRole("button", { name: "Undo" })));
    expect(localStorage.getItem(tokenKey(ADDRESS))).toBeNull();
    expect(toasts()).toEqual([]);
  } finally {
    cleanup();
    jest.advanceTimersByTime(0);
    jest.useRealTimers();
  }
});

test("a Save token toast stays visible at 5,999 ms and closes at 6,000 ms", async () => {
  await mount();
  const field = within(card()).getByLabelText("Serve token for writer-host", { selector: "input" });
  await act(async () => void fireEvent.change(field, { target: { value: "abc123" } }));
  jest.useFakeTimers();
  try {
    await act(async () => void fireEvent.click(within(card()).getByRole("button", { name: "Save token" })));
    expect(toast()).toBe(savedToast);
    await act(async () => void jest.advanceTimersByTime(5999));
    expect(toast()).toBe(savedToast);
    await act(async () => void jest.advanceTimersByTime(1));
    expect(toasts()).toEqual([]);
  } finally {
    cleanup();
    jest.advanceTimersByTime(0);
    jest.useRealTimers();
  }
});
