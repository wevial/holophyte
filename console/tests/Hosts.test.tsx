import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { App } from "../src/App";
import { WRITES_LATER } from "../src/components/ActionButton";
import { KEY_GLYPH } from "../src/components/HostCard";
import { Hosts } from "../src/components/Hosts";
import { Rail } from "../src/components/Rail";
import { tokenedFetch, type Fetch } from "../src/lib/poll";
import { TOKEN_KEY_PREFIX, storeToken } from "../src/lib/token";
import type { Status } from "../src/lib/types";
import { NO_ATTENTION, fakeDeps, fixture, hostOf, peersFetch, settle } from "./harness";

const working = await fixture<Status>("working.json");
const staleSupervisor = await fixture<Status>("stale_supervisor.json");
const second = await fixture<Status>("idle_second_host.json");

beforeEach(() => localStorage.clear());
afterEach(cleanup);

const withDaemon = (status: Status, upMs: number): Status => ({
  ...status,
  daemon: { started_ms: status.now - upMs, pid: 4000 },
});

test("a stale supervisor reads stale · pid · hb in bold bad; a live one reads in the ok colour", () => {
  const DAY = 86_400_000;
  const hosts = [
    hostOf(withDaemon(staleSupervisor, 3 * DAY + 4 * 3_600_000), NO_ATTENTION, "http://writer:7710"),
    hostOf(withDaemon(second, 11 * 3_600_000), NO_ATTENTION, "http://writer-2:7710"),
  ];
  render(<Hosts hosts={hosts} project="all" now={0} />);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Hosts & daemons");
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("2 hosts · 2 daemons on :7710");

  const stale = screen.getByRole("article", { name: "writer" });
  const staleCell = stale.querySelector("[data-supervisor]")!;
  expect(staleCell.textContent).toBe("stale · pid 4242 · hb 4m");
  expect(staleCell.getAttribute("data-supervisor")).toBe("stale");
  expect(staleCell.className).toContain("text-bad-text");
  expect(staleCell.className).toContain("font-semibold");
  expect(stale.querySelector("[data-daemon]")!.textContent).toBe("up 3d 4h");
  expect(stale.querySelector("[data-runs]")!.textContent).toBe("0 active");

  const live = screen.getByRole("article", { name: "writer-2" });
  const liveCell = live.querySelector("[data-supervisor]")!;
  expect(liveCell.textContent).toBe("live · pid 4343 · hb 9s");
  expect(liveCell.className).toContain("text-ok-text");
  expect(liveCell.className).not.toContain("font-semibold");
  expect(live.querySelector("[data-daemon]")!.textContent).toBe("up 11h");
  expect(within(live).getByText("/srv/dev/writer-2")).toBeTruthy();

  const actions = within(live).getAllByRole("button");
  expect(actions.map((button) => button.textContent)).toEqual(["Restart supervisor", "Open daemon log"]);
  for (const button of actions) {
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.getAttribute("title")).toBe(WRITES_LATER);
  }
});

test("an unreachable daemon's card says so with the last good answer's age in place of the cells", () => {
  const lost = { ...hostOf(working, NO_ATTENTION, "http://writer:7710", 1_000), error: "connection refused", polled_ms: 41_000 };
  render(<Hosts hosts={[lost]} project="all" now={41_000} />);
  const card = screen.getByRole("article", { name: "writer" });
  expect(card.getAttribute("data-unreachable")).toBe("true");
  expect(card.querySelector("[data-unreachable-line]")!.textContent).toBe("unreachable · last seen 40s ago");
  expect(card.querySelector("[data-supervisor]")).toBeNull();
  expect(card.className).toContain("border-bad/50");
});

test("the selected project keeps only its daemon's card", () => {
  const hosts = [hostOf(working, NO_ATTENTION, "http://writer:7710"), hostOf(second, NO_ATTENTION, "http://writer-2:7710")];
  render(<Hosts hosts={hosts} project="/srv/dev/writer-2" now={0} />);
  expect(screen.getAllByRole("article").map((card) => card.getAttribute("aria-label"))).toEqual(["writer-2"]);
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("1 host · 1 daemon on :7710");
});

test("a daemon that answered 401 gets the Token field in its card and the key glyph in the rail, with no unreachable styling", () => {
  const asking = { ...hostOf(working, NO_ATTENTION, "http://writer:7710", 1_000), status: null, project: null, seen_ms: null, needs_token: true };
  render(<Hosts hosts={[asking]} project="all" now={1_000} />);
  const card = screen.getByRole("article", { name: "writer" });
  expect(card.getAttribute("data-needs-token")).toBe("true");
  expect(card.getAttribute("data-unreachable")).toBeNull();
  expect(card.className).not.toContain("border-bad");
  expect(card.querySelector("[data-unreachable-line]")).toBeNull();
  const field = within(card).getByLabelText("Token") as HTMLInputElement;
  expect(field.type).toBe("password");
  expect(field.getAttribute("autocomplete")).toBe("off");
  const use = within(card).getByRole("button", { name: "Use" }) as HTMLButtonElement;
  expect(use.disabled).toBe(false);
  expect(use.getAttribute("title")).toBeNull();
  cleanup();

  const peers = { hosts: [asking], polledAgo: 0, polls: 1, now: 1_000 };
  render(<Rail peers={peers} view="hosts" onView={() => {}} project="all" onProject={() => {}} theme="system" onTheme={() => {}} />);
  const entry = screen.getByRole("region", { name: "Hosts" }).querySelector("[data-host]")!;
  expect(entry.getAttribute("data-needs-token")).toBe("true");
  expect(entry.getAttribute("data-unreachable")).toBeNull();
  expect(entry.className).not.toContain("border-bad");
  expect(within(entry as HTMLElement).getByRole("img", { name: "needs token" }).textContent).toBe(KEY_GLYPH);
  expect(entry.textContent).not.toContain("unreachable");
  expect(screen.queryByRole("alert")).toBeNull();
});

/** The leaf elements whose markup carries `text`: the password field alone
 *  while a token is typed, none once it is stored. */
const carriers = (text: string) =>
  Array.from(document.body.querySelectorAll("*")).filter((element) => element.children.length === 0 && element.outerHTML.includes(text));

test("submitting the field stores the token for that address only, the next poll carries it, and a later 401 forgets it", async () => {
  const ORIGIN = "http://writer:7710";
  const PEER = "http://writer-2:7710";
  const TOKEN = "hunter2-serve-token";
  const good = peersFetch(ORIGIN, {
    [ORIGIN]: { status: working, attention: NO_ATTENTION },
    [PEER]: { status: second, attention: NO_ATTENTION },
  });
  // The peer is the tailnet-bound daemon: every JSON route but /peers
  // wants the exact bearer value, and what it accepts can change under
  // the page, as a rotated token file would.
  let accepted = TOKEN;
  const seen: { url: string; authorization: string | null }[] = [];
  const daemon: Fetch = (url, init) => {
    const authorization = new Headers(init?.headers).get("authorization");
    seen.push({ url, authorization });
    if (url.startsWith(PEER) && !url.endsWith("/peers") && authorization !== `Bearer ${accepted}`) {
      return Promise.resolve(Response.json({}, { status: 401 }));
    }
    return good(url, init);
  };
  const { deps, firePoll } = fakeDeps(tokenedFetch(daemon));
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "Hosts" }));
  const card = () => screen.getByRole("article", { name: "writer-2" });
  expect(card().getAttribute("data-needs-token")).toBe("true");
  expect(screen.getByRole("article", { name: "writer" }).getAttribute("data-needs-token")).toBeNull();
  expect(seen.every((request) => request.authorization == null)).toBe(true);

  fireEvent.change(within(card()).getByLabelText("Token"), { target: { value: TOKEN } });
  // While typed, the value is in the field and nowhere else in the page.
  const field = within(card()).getByLabelText("Token") as HTMLInputElement;
  expect(field.value).toBe(TOKEN);
  expect(field.type).toBe("password");
  expect(carriers(TOKEN)).toEqual([field]);
  expect(document.body.textContent).not.toContain(TOKEN);
  fireEvent.submit(within(card()).getByLabelText("Token").closest("form")!);
  expect(Object.keys(localStorage)).toEqual([`${TOKEN_KEY_PREFIX}writer-2:7710`]);
  expect(localStorage.getItem(`${TOKEN_KEY_PREFIX}writer-2:7710`)).toBe(TOKEN);
  expect(card().querySelector("[data-token-sent]")).not.toBeNull();
  expect(within(card()).queryByLabelText("Token")).toBeNull();

  seen.length = 0;
  await act(async () => {
    firePoll();
    await settle();
  });
  const toPeer = seen.filter((request) => request.url.startsWith(PEER));
  expect(toPeer.map((request) => request.url)).toContain(`${PEER}/status`);
  expect(toPeer.map((request) => request.url)).toContain(`${PEER}/attention`);
  expect(toPeer.every((request) => request.authorization === `Bearer ${TOKEN}`)).toBe(true);
  expect(seen.filter((request) => request.url.startsWith(ORIGIN)).every((request) => request.authorization == null)).toBe(true);
  expect(card().getAttribute("data-needs-token")).toBeNull();
  expect(card().querySelector("[data-supervisor]")!.textContent).toBe("live · pid 4343 · hb 9s");
  expect(within(card()).queryByLabelText("Token")).toBeNull();
  expect(carriers(TOKEN)).toEqual([]);
  expect(seen.every((request) => !request.url.includes(TOKEN))).toBe(true);

  // The daemon stops accepting the token: one 401 forgets it and the
  // field is back; the poll after that carries no header.
  accepted = "rotated";
  await act(async () => {
    firePoll();
    await settle();
  });
  expect(localStorage.length).toBe(0);
  expect(card().getAttribute("data-needs-token")).toBe("true");
  expect((within(card()).getByLabelText("Token") as HTMLInputElement).value).toBe("");
  seen.length = 0;
  await act(async () => {
    firePoll();
    await settle();
  });
  expect(seen.length).toBeGreaterThan(0);
  expect(seen.every((request) => request.authorization == null)).toBe(true);
  expect(seen.every((request) => !request.url.includes(TOKEN))).toBe(true);
  expect(carriers(TOKEN)).toEqual([]);
});

test("a token stored before the page loads rides the first poll, so the card never asks", async () => {
  const ORIGIN = "http://writer:7710";
  const daemon: Fetch = (url, init) => {
    if (!url.endsWith("/peers") && new Headers(init?.headers).get("authorization") !== "Bearer kept") {
      return Promise.resolve(Response.json({}, { status: 401 }));
    }
    return peersFetch(ORIGIN, { [ORIGIN]: { status: working, attention: NO_ATTENTION } })(url, init);
  };
  storeToken("writer:7710", "kept");
  const { deps } = fakeDeps(tokenedFetch(daemon));
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "Hosts" }));
  const card = screen.getByRole("article", { name: "writer" });
  expect(card.getAttribute("data-needs-token")).toBeNull();
  expect(within(card).queryByLabelText("Token")).toBeNull();
});

test("a token given to the origin is keyed by the address the page requests, not the one the daemon advertises as self", async () => {
  // The page is opened at http://writer:7710; the daemon's /peers names
  // itself by its tailnet address. Store, header and forget must all use
  // the request address, or the header never appears after submission.
  const ORIGIN = "http://writer:7710";
  const SELF = "100.64.0.10:7710";
  const TOKEN = "tailnet-serve-token";
  let accepted = TOKEN;
  const seen: { url: string; authorization: string | null }[] = [];
  const daemon: Fetch = (url, init) => {
    const authorization = new Headers(init?.headers).get("authorization");
    seen.push({ url, authorization });
    if (url.endsWith("/peers")) return Promise.resolve(Response.json({ self: SELF, peers: [] }));
    if (authorization !== `Bearer ${accepted}`) return Promise.resolve(Response.json({}, { status: 401 }));
    if (url.endsWith("/status")) return Promise.resolve(Response.json(working));
    if (url.endsWith("/attention")) return Promise.resolve(Response.json(NO_ATTENTION));
    return Promise.resolve(new Response("not found", { status: 404 }));
  };
  const { deps, firePoll } = fakeDeps(tokenedFetch(daemon));
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "Hosts" }));
  const card = () => document.querySelector<HTMLElement>(`article[data-host="${SELF}"]`)!;
  expect(card()).not.toBeNull();
  expect(card().getAttribute("data-needs-token")).toBe("true");

  fireEvent.change(within(card()).getByLabelText("Token"), { target: { value: TOKEN } });
  fireEvent.submit(within(card()).getByLabelText("Token").closest("form")!);
  expect(Object.keys(localStorage)).toEqual([`${TOKEN_KEY_PREFIX}writer:7710`]);

  seen.length = 0;
  await act(async () => {
    firePoll();
    await settle();
  });
  const json = seen.filter((request) => !request.url.endsWith("/peers"));
  expect(json.map((request) => request.url)).toContain(`${ORIGIN}/status`);
  expect(json.map((request) => request.url)).toContain(`${ORIGIN}/attention`);
  expect(json.every((request) => request.authorization === `Bearer ${TOKEN}`)).toBe(true);
  expect(card().getAttribute("data-needs-token")).toBeNull();

  // A 401 forgets the key the fetch seam reads, so the next poll is bare.
  accepted = "rotated";
  await act(async () => {
    firePoll();
    await settle();
  });
  expect(localStorage.length).toBe(0);
  expect(card().getAttribute("data-needs-token")).toBe("true");
  seen.length = 0;
  await act(async () => {
    firePoll();
    await settle();
  });
  expect(seen.length).toBeGreaterThan(0);
  expect(seen.every((request) => request.authorization == null)).toBe(true);
});
