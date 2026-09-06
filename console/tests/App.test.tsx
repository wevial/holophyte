import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { App } from "../src/App";
import { THEME_KEY } from "../src/lib/theme";
import type { Attention, Status } from "../src/lib/types";
import type { Fetch } from "../src/lib/poll";
import { NO_ATTENTION, fakeDeps, fixture, peersFetch, settle, stubFetch } from "./harness";

const BASE = "http://writer:7710";

const themeCss = await Bun.file(new URL("../src/theme.css", import.meta.url)).text();

const working = await fixture<Status>("working.json");
const idle = await fixture<Status>("idle.json");
const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");

async function mount(bodies: { status: Status; attention: Attention }) {
  const { deps } = fakeDeps(stubFetch(bodies));
  render(<App base={BASE} pollDeps={deps} />);
  await act(settle);
}

/** happy-dom's device settings; `prefersColorScheme` feeds its
 *  `@media (prefers-color-scheme)` evaluation. */
const device = (window as unknown as { happyDOM: { settings: { device: { prefersColorScheme: string } } } })
  .happyDOM.settings.device;

/** Attach theme.css (minus the Tailwind import, which the plugin resolves at
 *  build time) so `getComputedStyle` answers with the live token values. */
function loadThemeCss() {
  const style = document.createElement("style");
  style.textContent = themeCss.replace(/@import[^;]*;/, "");
  document.head.appendChild(style);
  return style;
}

const token = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => {
  cleanup();
  device.prefersColorScheme = "light";
  document.head.querySelectorAll("style").forEach((style) => style.remove());
});

test("the rail lists the project from the daemon's path and the four views", async () => {
  await mount({ status: working, attention: NO_ATTENTION });
  const projects = screen.getByRole("region", { name: "Projects" });
  expect(within(projects).getByText("All projects")).toBeTruthy();
  expect(within(projects).getByText("writer")).toBeTruthy();
  expect(within(projects).getByText("writer · supervisor live")).toBeTruthy();
  const views = screen.getByRole("region", { name: "Views" });
  expect(within(views).getAllByRole("button").map((button) => button.textContent)).toEqual([
    "Now",
    "Board",
    "Hosts",
    "Shipped",
  ]);
  const hosts = screen.getByRole("region", { name: "Hosts" });
  expect(within(hosts).getByText(":7710 · hb 12s")).toBeTruthy();
  expect(within(hosts).getByText("1 run")).toBeTruthy();
});

test("clicking Shipped selects it and changes the main heading", async () => {
  await mount({ status: working, attention: NO_ATTENTION });
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Now");
  const shipped = screen.getByRole("button", { name: "Shipped" });
  expect(shipped.getAttribute("aria-pressed")).toBe("false");
  fireEvent.click(shipped);
  expect(shipped.getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: /^Now/ }).getAttribute("aria-pressed")).toBe("false");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Shipped");
});

test("the Now button carries the attention count as a badge", async () => {
  await mount(allKinds);
  const views = screen.getByRole("region", { name: "Views" });
  const now = within(views).getAllByRole("button")[0]!;
  const badge = within(now).getByText(String(allKinds.attention.items.length));
  expect(badge.getAttribute("aria-label")).toBe(`${allKinds.attention.items.length} needing you`);
  // The stale supervisor in this fixture marks the host card.
  const hosts = screen.getByRole("region", { name: "Hosts" });
  expect(hosts.querySelector("[data-stale]")).toBeTruthy();
});

test("with nothing needing attention the Now button has no badge", async () => {
  await mount({ status: idle, attention: NO_ATTENTION });
  const views = screen.getByRole("region", { name: "Views" });
  const now = within(views).getAllByRole("button")[0]!;
  expect(now.textContent).toBe("Now");
  expect(now.querySelector("[aria-label]")).toBeNull();
  expect(screen.getByRole("region", { name: "Hosts" }).querySelector("[data-stale]")).toBeNull();
});

test("no stored theme under a dark system preference leaves the document unstamped with the dark tokens; choosing Light stamps and persists it", async () => {
  device.prefersColorScheme = "dark";
  loadThemeCss();
  await mount({ status: idle, attention: NO_ATTENTION });
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(screen.getByRole("button", { name: "System" }).getAttribute("aria-pressed")).toBe("true");
  // The dark set applies through the media query alone.
  expect(token("--paper")).toBe("#141210");
  expect(token("--ink")).toBe("#ece7dc");
  fireEvent.click(screen.getByRole("button", { name: "Light" }));
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem(THEME_KEY)).toBe("light");
  // The stamp overrides the system preference: paper tokens now apply.
  expect(token("--paper")).toBe("#f4f1ea");
  expect(token("--ink")).toBe("#1d1b17");
  expect(screen.getByRole("button", { name: "Light" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "System" }));
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(localStorage.getItem(THEME_KEY)).toBeNull();
});

const second = await fixture<Status>("idle_second_host.json");
const ORIGIN = "http://writer:7710";
const PEER = "http://writer-2:7710";

const hostCards = () =>
  Array.from(screen.getByRole("region", { name: "Hosts" }).querySelectorAll("[data-host]")).map((card) => ({
    address: card.getAttribute("data-host"),
    heartbeat: card.querySelector("[data-heartbeat]")!.textContent,
    unreachable: card.hasAttribute("data-unreachable"),
    border: card.className.includes("border-bad/50"),
    second: card.lastElementChild!.textContent,
  }));

const projectRows = () =>
  within(screen.getByRole("region", { name: "Projects" }))
    .getAllByRole("button")
    .map((row) => row.textContent);

test("two daemons in /peers: the rail lists both hosts with their heartbeats and two project rows with live run counts", async () => {
  const fetchImpl = peersFetch(ORIGIN, {
    [ORIGIN]: { status: working, attention: NO_ATTENTION },
    [PEER]: { status: second, attention: NO_ATTENTION },
  });
  const { deps } = fakeDeps(fetchImpl);
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  expect(hostCards()).toEqual([
    { address: "writer:7710", heartbeat: ":7710 · hb 12s", unreachable: false, border: false, second: "1 run" },
    { address: "writer-2:7710", heartbeat: ":7710 · hb 9s", unreachable: false, border: false, second: "0 runs" },
  ]);
  expect(projectRows()).toEqual(["All projects", "writerwriter · supervisor live1", "writer-2writer-2 · supervisor live0"]);
  // "All projects" shows every run on the Floor: the total across both daemons.
  expect(screen.getByText("1 run · 2 projects")).toBeTruthy();
});

test("a peer that times out reads unreachable with the bad border, keeps its last status with last seen, and adds one critical item", async () => {
  let peerDown = false;
  const good = peersFetch(ORIGIN, {
    [ORIGIN]: { status: working, attention: NO_ATTENTION },
    [PEER]: { status: second, attention: NO_ATTENTION },
  });
  const fetchImpl: Fetch = (url, init) => {
    if (peerDown && url.startsWith(PEER)) {
      return Promise.reject(new DOMException("The operation timed out", "TimeoutError"));
    }
    return good(url, init);
  };
  const { deps, clock, firePoll } = fakeDeps(fetchImpl);
  clock.now = 1_000_000;
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  expect(screen.queryByRole("alert")).toBeNull();

  peerDown = true;
  clock.now = 1_040_000;
  await act(async () => {
    firePoll();
    await settle();
  });
  const [, lost] = hostCards();
  expect(lost).toEqual({
    address: "writer-2:7710",
    heartbeat: "unreachable",
    unreachable: true,
    border: true,
    second: "last seen 40s ago · 0 runs",
  });
  expect(screen.getByRole("alert").textContent).toBe("poll failed: http://writer-2:7710/status timed out");
  // The project row stays, on the faint dot, since its last status is kept.
  expect(projectRows()[2]).toBe("writer-2writer-2 · supervisor live0");
  // The Now view counts the lost daemon as one critical thing needing you.
  const band = screen.getByRole("region", { name: "Needs you" });
  expect(within(band).getByText("1").hasAttribute("data-count")).toBe(true);
  const row = within(band).getByRole("listitem");
  expect(row.getAttribute("data-kind")).toBe("unreachable");
  expect(within(row).getByText("writer-2 is not answering")).toBeTruthy();
  expect(within(row).getByText("writer-2:7710 · last seen 40s ago · http://writer-2:7710/status timed out")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "Views" })).getByText("1").getAttribute("aria-label")).toBe("1 needing you");
});

test("selecting a project in the rail narrows the Floor and the Hosts view to its daemon; All projects restores both", async () => {
  const fetchImpl = peersFetch(ORIGIN, {
    [ORIGIN]: { status: working, attention: NO_ATTENTION },
    [PEER]: { status: { ...second, runs: [{ ...working.runs[0]!, id: 7, ticket: "KO-7", host: "writer-2" }] }, attention: NO_ATTENTION },
  });
  const { deps } = fakeDeps(fetchImpl);
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  const floorBlocks = () => Array.from(screen.getByRole("region", { name: "Floor" }).querySelectorAll("section")).map((block) => block.getAttribute("aria-label"));
  expect(floorBlocks()).toEqual(["writer", "writer-2"]);

  fireEvent.click(screen.getByRole("button", { name: /^writer-2/ }));
  expect(floorBlocks()).toEqual(["writer-2"]);
  expect(screen.getByText("1 run · 1 project")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Hosts" }));
  expect(screen.getAllByRole("article").map((card) => card.getAttribute("aria-label"))).toEqual(["writer-2"]);

  fireEvent.click(screen.getByRole("button", { name: "All projects" }));
  expect(screen.getAllByRole("article").map((card) => card.getAttribute("aria-label"))).toEqual(["writer", "writer-2"]);
  fireEvent.click(screen.getByRole("button", { name: /^Now/ }));
  expect(floorBlocks()).toEqual(["writer", "writer-2"]);
});

test("both daemons failing on the first poll: Now still opens with one critical unreachable row per daemon", async () => {
  const fetchImpl = peersFetch(ORIGIN, {
    [ORIGIN]: { down: new DOMException("The operation timed out", "TimeoutError") },
    [PEER]: { down: new TypeError("Failed to fetch") },
  });
  const { deps } = fakeDeps(fetchImpl);
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  expect(hostCards().map((card) => card.heartbeat)).toEqual(["unreachable", "unreachable"]);
  expect(screen.queryByText("Nothing to show here yet.")).toBeNull();
  const band = screen.getByRole("region", { name: "Needs you" });
  expect(within(band).getByText("2").hasAttribute("data-count")).toBe(true);
  const rows = within(band).getAllByRole("listitem");
  expect(rows.map((row) => row.getAttribute("data-kind"))).toEqual(["unreachable", "unreachable"]);
  expect(within(rows[0]!).getByText("writer:7710 · never answered · http://writer:7710/status timed out")).toBeTruthy();
  expect(within(rows[1]!).getByText("writer-2:7710 · never answered · Failed to fetch")).toBeTruthy();
});
