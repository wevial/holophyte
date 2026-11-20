import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { ACTIONS_OFF } from "../../src/lib/actions";
import { THEME_KEY } from "../../src/lib/theme";
import { APP_SHELL_KEY } from "../../src/lib/view";
import { TICK_MS, type Fetch } from "../../src/lib/poll";
import type { Attention, Board, Flow, MergeQueue, Status } from "../../src/lib/contract.gen";
import { fakeDeps, peersFetch, settle } from "../harness";
import { needsDaemons } from "./needsFleet";
import { setPlatform } from "@ui-sdk/core/testing";

const ORIGIN = "http://writer-host:7710";
const PEER = "http://writer-host:7711";

const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;
const servedQueue = (await Bun.file(new URL("merge-queue.json", SERVE)).json()) as MergeQueue;
const noQueue: MergeQueue = { ...servedQueue, configured: false, configuration: null, entries: [], left: [] };

const happyDOM = (window as unknown as {
  happyDOM: {
    settings: { device: { prefersColorScheme: string } };
    setViewport: (viewport: { width: number }) => void;
    setURL: (url: string) => void;
  };
}).happyDOM;
const device = happyDOM.settings.device;

type Item = Attention["items"][number];
const statusOf = (project: string): Status => ({ ...working, host: "writer-host", project });
const attentionOf = (items: Item[], project = "/srv/dev/demo"): Attention => ({ level: items.length ? "attention" : "none", now: 0, project, items });

function daemons(demoItems: Item[], writerItems: Item[]) {
  return peersFetch(ORIGIN, {
    [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: attentionOf(demoItems) },
    [PEER]: { status: statusOf("/srv/dev/writer"), attention: attentionOf(writerItems, "/srv/dev/writer") },
  });
}

async function mount(hash: string, fetchImpl: Fetch = daemons([], [])) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={ORIGIN} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  await act(settle);
  await act(settle);
  return fake;
}

const pages = () => within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("link").filter((link) => !link.closest("ul"));
const badge = () => screen.getByRole("navigation", { name: "Pages" }).querySelector("[data-badge]");

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  window.location.hash = "";
  happyDOM.setURL("about:blank");
  device.prefersColorScheme = "light";
});

test("a scoped hash opens its page, marks its link current and keeps the scope on every page link", async () => {
  await mount("#/runs?project=demo");
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Runs");
  const links = pages();
  expect(links.map((link) => link.getAttribute("aria-label"))).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts", "Flow"]);
  expect(links.filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.textContent)).toEqual(["Runs"]);
  for (const link of links) {
    expect(new URLSearchParams(link.getAttribute("href")!.split("?")[1]).get("project")).toBe("demo");
  }
});

test("the Overview badge counts the scope's Needs you entries, as the heading does, and hides at zero", async () => {
  await mount("#/", needsDaemons());
  expect(screen.getByRole("link", { name: "Overview, 2 waiting on you" })).toBeTruthy();
  expect(badge()!.textContent).toBe("2");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("2 need you");
  cleanup();

  await mount("#/?project=demo", needsDaemons());
  expect(screen.getByRole("link", { name: "Overview, 2 waiting on you" })).toBeTruthy();
  expect(badge()!.textContent).toBe("2");
  cleanup();

  await mount("#/");
  expect(screen.getByRole("link", { name: "Overview" })).toBeTruthy();
  expect(badge()).toBeNull();
});

function queueDaemons(demo: MergeQueue, writer: MergeQueue): Fetch {
  const polls = daemons([], []);
  return async (url, init) => {
    if (url === `${ORIGIN}/merge-queue`) return Response.json(demo);
    if (url === `${PEER}/merge-queue`) return Response.json(writer);
    return polls(url, init);
  };
}

const labels = () => pages().map((link) => link.getAttribute("aria-label"));
const queueBadge = () => screen.getByRole("link", { name: /^Merge queue/ }).querySelector("[data-badge]")!;

test("the Merge queue link shows after Runs only while a polled project has a queue, its badge the entries in the live tone", async () => {
  await mount("#/", queueDaemons(noQueue, noQueue));
  expect(labels()).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts", "Flow"]);
  cleanup();

  const quiet = { ...servedQueue, left: servedQueue.left.map((row) => ({ ...row, waits_on_you: false })) };
  await mount("#/", queueDaemons(noQueue, quiet));
  expect(labels()).toEqual(["Overview", "Runs", "Merge queue, 4 queued", "Tickets", "Findings", "Scouts", "Flow"]);
  expect(queueBadge().textContent).toBe("4");
  expect(queueBadge().classList.contains("text-live")).toBe(true);
  expect(queueBadge().classList.contains("text-need")).toBe(false);
});

test("the Merge queue link shows only for a scope with a queue: the project that has one, or All projects", async () => {
  await mount("#/?project=demo", queueDaemons(noQueue, servedQueue));
  expect(labels()).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts", "Flow"]);
  cleanup();

  for (const hash of ["#/?project=writer", "#/"]) {
    await mount(hash, queueDaemons(noQueue, servedQueue));
    expect(labels()).toEqual(["Overview", "Runs", "Merge queue, 4 queued", "Tickets", "Findings", "Scouts", "Flow"]);
    cleanup();
  }
});

test("the Merge queue badge takes the need tone while a pull request that left the queue waits on you", async () => {
  expect(servedQueue.left.some((row) => row.waits_on_you)).toBe(true);
  await mount("#/", queueDaemons(noQueue, servedQueue));
  expect(queueBadge().textContent).toBe("4");
  expect(queueBadge().classList.contains("text-need")).toBe(true);
  expect(queueBadge().classList.contains("text-live")).toBe(false);
});

test("an unknown route links back to Overview", async () => {
  await mount("#/nowhere");
  expect(await screen.findByText("There is no page here.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Go to Overview" }).getAttribute("href")).toMatch(/#\/$/);
});

test("the sidebar draws no line of its own, and the page panel keeps the canvas behind a hairline", async () => {
  await mount("#/");
  const sidebar = screen.getByRole("complementary", { name: "Console" }).classList;
  expect(sidebar.contains("border-r")).toBe(false);
  const main = screen.getByRole("main").classList;
  for (const name of ["bg-canvas", "min-[720px]:rounded-tl-[10px]", "min-[720px]:shadow-[0_0_0_1px_var(--rule-2)]"]) expect(main.contains(name)).toBe(true);
  expect(main.contains("bg-side")).toBe(false);
});

test("the sidebar footer, folded or not, holds Settings and the theme toggle and no Classic console link", async () => {
  await mount("#/");
  for (const fold of [false, true]) {
    if (fold) fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    const footer = screen.getByRole("complementary", { name: "Console" }).querySelector("footer")!;
    expect(within(footer).getByRole("link", { name: "Settings" })).toBeTruthy();
    expect(within(footer).getByRole("group", { name: "Theme" })).toBeTruthy();
    expect(within(footer).queryByRole("button", { name: /colour theme|switch to/i })).toBeNull();
    expect([...footer.children]).toEqual([
      footer.querySelector("[data-connection]")!,
      within(footer).getByRole("group", { name: "Theme" }),
      within(footer).getByRole("link", { name: "Settings" }),
    ]);
    expect(screen.queryByRole("link", { name: /classic/i })).toBeNull();
    expect(document.querySelector('a[href*="classic"]')).toBeNull();
  }
});

test("the fold button folds the sidebar to icons, keeps link names and survives a remount and failing storage", async () => {
  await mount("#/");
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  const expand = screen.getByRole("button", { name: "Expand sidebar" });
  expect(expand.getAttribute("aria-expanded")).toBe("false");
  expect(pages().map((link) => link.textContent)).toEqual(["", "", "", "", "", ""]);
  expect(pages().map((link) => link.getAttribute("aria-label"))).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts", "Flow"]);
  cleanup();

  await mount("#/");
  expect(screen.getByRole("button", { name: "Expand sidebar" })).toBeTruthy();
  cleanup();

  localStorage.clear();
  const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
  let touched = 0;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get: () => {
      touched += 1;
      throw new Error("storage disabled");
    },
  });
  try {
    await mount("#/");
    const beforeFold = touched;
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(screen.getByRole("button", { name: "Expand sidebar" }).getAttribute("aria-expanded")).toBe("false");
    expect(touched).toBeGreaterThan(beforeFold);
  } finally {
    Object.defineProperty(globalThis, "localStorage", real);
  }
});

const themeGroup = () => screen.getByRole("group", { name: "Theme" });
const pressed = () => within(themeGroup()).getAllByRole("button").filter((button) => button.getAttribute("aria-pressed") === "true").map((button) => button.getAttribute("aria-label"));

test("the theme group's Dawn, Night and System buttons stamp and store the choice and press only it", async () => {
  await mount("#/");
  const buttons = within(themeGroup()).getAllByRole("button");
  expect(buttons.map((button) => button.getAttribute("aria-label"))).toEqual(["Dawn", "Night", "System"]);
  expect(buttons.map((button) => button.getAttribute("title"))).toEqual(["Dawn", "Night", "System"]);
  expect(buttons.map((button) => button.textContent)).toEqual(["", "", ""]);
  expect(buttons.map((button) => button.getAttribute("aria-pressed"))).toEqual(["false", "false", "true"]);
  const choose = (name: string) => fireEvent.click(within(themeGroup()).getByRole("button", { name }));

  choose("Dawn");
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem(THEME_KEY)).toBe("light");
  expect(pressed()).toEqual(["Dawn"]);

  choose("Night");
  expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  expect(localStorage.getItem(THEME_KEY)).toBe("dark");
  expect(pressed()).toEqual(["Night"]);

  choose("System");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(localStorage.getItem(THEME_KEY)).toBeNull();
  expect(pressed()).toEqual(["System"]);
});

test("folded or not, the Theme group holds Dawn, Night and System alone, draws no border or ground, and only the pressed one sits on selected", async () => {
  await mount("#/");
  for (const fold of [false, true]) {
    if (fold) fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect([...themeGroup().children].map((child) => child.getAttribute("aria-label"))).toEqual(["Dawn", "Night", "System"]);
    expect([...themeGroup().classList].filter((name) => /(^|:)(border|bg|ring|shadow)(-|$)/.test(name))).toEqual([]);
    const onSelected = within(themeGroup()).getAllByRole("button").filter((button) => button.classList.contains("bg-selected"));
    expect(onSelected.map((button) => button.getAttribute("aria-label"))).toEqual(pressed());
    expect(pressed()).toEqual([fold ? "Dawn" : "System"]);
    fireEvent.click(within(themeGroup()).getByRole("button", { name: "Dawn" }));
  }
});

test("a stored Night presses Night, and with nothing stored System stays pressed across a system preference change", async () => {
  localStorage.setItem(THEME_KEY, "dark");
  await mount("#/");
  expect(pressed()).toEqual(["Night"]);
  cleanup();
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");

  device.prefersColorScheme = "dark";
  await mount("#/");
  expect(pressed()).toEqual(["System"]);
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  device.prefersColorScheme = "light";
  try {
    act(() => happyDOM.setViewport({ width: 1000 }));
    expect(pressed()).toEqual(["System"]);
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.getItem(THEME_KEY)).toBeNull();
  } finally {
    happyDOM.setViewport({ width: 1024 });
  }
});

const tooltip = () => screen.queryByRole("tooltip");
const keyboardFocus = (link: HTMLElement) => act(async () => {
  fireEvent.keyDown(document.body, { key: "Tab" });
  link.focus();
});
const pointerOver = async (link: HTMLElement) => {
  await act(async () => void fireEvent.pointerEnter(link, { pointerType: "mouse" }));
  await act(() => new Promise((resolve) => setTimeout(resolve, 400)));
};
const pointerAway = async (link: HTMLElement) => {
  await act(async () => void fireEvent.pointerLeave(link, { pointerType: "mouse", clientX: 900, clientY: 900 }));
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
};

const connectionDot = () => screen.getByRole("complementary", { name: "Console" }).querySelector<HTMLElement>("[data-connection]")!;
const breathes = () => connectionDot().querySelector(".state-pulse") != null;

test("before any answer the connection dot is stale and still and named polling, and with every host refusing it is down", async () => {
  const pending: Fetch = () => new Promise(() => {});
  await mount("#/", pending);
  expect(connectionDot().getAttribute("data-connection")).toBe("stale");
  expect(connectionDot().getAttribute("aria-label")).toBe("polling…");
  expect(breathes()).toBe(false);
  cleanup();

  await mount("#/", peersFetch(ORIGIN, { [ORIGIN]: { down: new Error("connection refused") } }));
  expect(connectionDot().getAttribute("data-connection")).toBe("down");
  expect(connectionDot().getAttribute("aria-label")).toBe("polling…");
  expect(breathes()).toBe(false);
});

test("with one of two hosts refusing, the connection dot is stale and still", async () => {
  await mount("#/", peersFetch(ORIGIN, {
    [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: attentionOf([]) },
    [PEER]: { down: new Error("connection refused") },
  }));
  expect(connectionDot().getAttribute("data-connection")).toBe("stale");
  expect(breathes()).toBe(false);
});

test("with the one host answering, the connection dot is live and breathing, its name and focus tooltip carry the footer sentence, and no sentence shows as text", async () => {
  const realSetInterval = globalThis.setInterval;
  const seconds: (() => void)[] = [];
  globalThis.setInterval = ((fn: () => void, ms?: number) => {
    if (ms === TICK_MS) seconds.push(fn);
    return realSetInterval(fn, ms);
  }) as typeof setInterval;
  try {
    const { clock } = await mount("#/", peersFetch(ORIGIN, {
      [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: attentionOf([]) },
    }));
    clock.now = 4000;
    act(() => seconds.forEach((tick) => tick()));
    const sentence = "writer-host · 1 project answering · polled 4s ago";
    expect(connectionDot().getAttribute("data-connection")).toBe("live");
    expect(breathes()).toBe(true);
    expect(screen.getByRole("img", { name: sentence })).toBe(connectionDot());
    expect(screen.getByRole("complementary", { name: "Console" }).querySelector("footer")!.textContent).toBe("");
    expect(tooltip()).toBeNull();
    await keyboardFocus(connectionDot());
    expect(tooltip()!.textContent).toBe(sentence);
  } finally {
    globalThis.setInterval = realSetInterval;
  }
});

test("expanded, the Settings link shows no text, keeps its name and tooltips Settings on keyboard focus", async () => {
  await mount("#/");
  const settings = within(screen.getByRole("complementary", { name: "Console" }).querySelector("footer")!).getByRole("link", { name: "Settings" });
  expect(settings.textContent).toBe("");
  expect(tooltip()).toBeNull();
  await keyboardFocus(settings);
  expect(tooltip()!.textContent).toBe("Settings");
});

test("the six page icons differ from one another, unfolded and folded", async () => {
  await mount("#/", peersFetch(ORIGIN, { [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: attentionOf([]) } }));
  const drawn = () => pages().map((link) => link.querySelector("svg")!.outerHTML);
  expect(new Set(drawn()).size).toBe(6);
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  expect(new Set(drawn()).size).toBe(6);
});

test("on Runs the Runs link alone paints its ground with the nav-on gradient and its icon in focus, and no page link takes the flat selected tone", async () => {
  await mount("#/runs");
  const names = (links: HTMLElement[]) => links.map((link) => link.getAttribute("aria-label"));
  expect(names(pages().filter((link) => link.classList.contains("bg-(image:--nav-on)")))).toEqual(["Runs"]);
  expect(names(pages().filter((link) => link.querySelector(".text-focus")))).toEqual(["Runs"]);
  expect(names(pages().filter((link) => [...link.classList].some((name) => name.endsWith("bg-selected"))))).toEqual([]);
});

test("expanded, Ready, Needs spec and Backlog are 27px links indented 34px with their counts, and nothing reads Views", async () => {
  await mount("#/tickets");
  expect(screen.queryByText("Views")).toBeNull();
  for (const name of ["Ready", "Needs spec", "Backlog"]) {
    const link = screen.getByRole("link", { name });
    expect([...link.classList].filter((token) => /^(h|pl)-\[/.test(token))).toEqual(["h-[27px]", "pl-[34px]"]);
    expect(link.firstElementChild!.textContent).toBe(name);
    expect(link.querySelector("[data-count]")!.textContent).toMatch(/^\d+$/);
  }
});

const DRAG = "[-webkit-app-region:drag]";
const NO_DRAG = "[-webkit-app-region:no-drag]";
const toggle = () => screen.getByRole("button", { name: /^(Collapse|Expand) sidebar$/ });
const titleBar = () => document.querySelector<HTMLElement>("[data-title-bar]")!;
const aside = () => screen.getByRole("complementary", { name: "Console" });

const homeLink = () => screen.getByRole("link", { name: "holophyte" });
const childNames = (row: Element) => Array.from(row.children).map((child) => child.getAttribute("aria-label"));

const expectBrand = (link: HTMLElement) => {
  expect(link.querySelector("svg")).not.toBeNull();
  expect(link.textContent).toBe("holophyte");
  expect(link.getAttribute("href")).toMatch(/#\/$/);
};

test("in a browser a 30px title bar spans the shell above the sidebar and the page panel, holding the fold toggle then the holophyte home link", async () => {
  await mount("#/");
  const bar = titleBar();
  const shell = bar.parentElement!;
  expect(shell.firstElementChild).toBe(bar);
  for (const name of ["h-[30px]", "pl-3"]) expect(bar.classList.contains(name)).toBe(true);
  expect(childNames(bar)).toEqual(["Collapse sidebar", "holophyte"]);
  expectBrand(homeLink());
  const body = bar.nextElementSibling!;
  expect(Array.from(body.children)).toEqual([aside(), screen.getByRole("main")]);
  expect(within(aside()).queryByRole("link", { name: "holophyte" })).toBeNull();
});

test("folded, the title bar keeps the toggle then the home link with its mark and name, and the rail narrows", async () => {
  await mount("#/");
  const expandedBar = titleBar().className;
  fireEvent.click(toggle());
  expect(titleBar().className).toBe(expandedBar);
  expect(titleBar().parentElement!.firstElementChild).toBe(titleBar());
  expect(childNames(titleBar())).toEqual(["Expand sidebar", "holophyte"]);
  expectBrand(homeLink());
  expect(aside().classList.contains("min-[720px]:w-[56px]")).toBe(true);
  expect(titleBar().nextElementSibling!.lastElementChild).toBe(screen.getByRole("main"));
  expect(within(aside()).queryByRole("link", { name: "holophyte" })).toBeNull();
});

/** The theme sheet as happy-dom can apply it: it drops every rule inside `@layer`, which only orders the cascade, so those blocks are lifted out
 *  with other at-rules keeping their conditions; and it cannot parse the grain's quoted data URLs, so each grain token holds a plain URL named after it. */
async function themeSheetForDom(): Promise<string> {
  const source = (await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text())
    .replace(/@import[^;]*;/, "")
    .replace(/(--grain(?:-side)?):[^;]*;/g, (_, name: string) => `${name}: url("${name.slice(2)}.svg");`);
  let out = "";
  let at = 0;
  for (const match of source.matchAll(/@layer [\w-]+\s*\{/g)) {
    let depth = 1;
    let end = match.index + match[0].length;
    for (; depth > 0; end += 1) depth += source[end] === "{" ? 1 : source[end] === "}" ? -1 : 0;
    out += source.slice(at, match.index) + source.slice(match.index + match[0].length, end - 1);
    at = end;
  }
  return out + source.slice(at);
}

test("in every theme the shell surface draws the side colour with the sidebar grain, and neither the title bar nor the sidebar paints its own", async () => {
  const style = document.createElement("style");
  style.textContent = await themeSheetForDom();
  document.head.appendChild(style);
  try {
    for (const theme of ["light", "dark"]) {
      localStorage.setItem(THEME_KEY, theme);
      await mount("#/");
      const shell = titleBar().parentElement!;
      expect(shell.classList.contains("bg-side")).toBe(true);
      expect(getComputedStyle(shell).backgroundImage).toBe('url("grain-side.svg")');
      for (const surface of [titleBar(), aside()]) {
        expect(Array.from(surface.classList).filter((name) => name.startsWith("bg-"))).toEqual([]);
        expect(getComputedStyle(surface).backgroundImage).toBe("");
      }
      expect(getComputedStyle(screen.getByRole("main")).backgroundImage).toBe('url("grain.svg")');
      cleanup();
      localStorage.clear();
    }
  } finally {
    style.remove();
  }
});

test("folded or expanded, the one sidebar toggle sits before the Pages navigation, and folding from it keeps focus on it", async () => {
  await mount("#/");
  const nav = () => screen.getByRole("navigation", { name: "Pages" });
  for (const name of ["Collapse sidebar", "Expand sidebar"]) {
    expect(screen.getAllByRole("button", { name: /^(Collapse|Expand) sidebar$/ }).length).toBe(1);
    expect(nav().compareDocumentPosition(toggle()) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
    const before = screen.getByRole("button", { name });
    await keyboardFocus(before);
    fireEvent.click(before);
    expect(document.activeElement === before && toggle() === before).toBe(true);
  }
  expect(screen.getAllByRole("button", { name: /^(Collapse|Expand) sidebar$/ }).length).toBe(1);
  expect(nav().compareDocumentPosition(toggle()) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
});

test("the sidebar toggle draws one icon in both states, unlike the story list's chevron", async () => {
  const polls = daemons([], []);
  const stories = [{ ticket: "DEMO-12", title: "Unattended runs recover themselves", children: [], merged: 2, total: 6 }];
  await mount("#/", async (url) => (url === `${ORIGIN}/board` ? Response.json({ ...pinned, stories }) : polls(url)));
  await act(settle);
  const expanded = toggle().querySelector("svg")!.outerHTML;
  const chevron = screen.getByRole("button", { name: "Hide the story list" }).querySelector("svg")!.outerHTML;
  fireEvent.click(toggle());
  expect(toggle().querySelector("svg")!.outerHTML).toBe(expanded);
  expect(expanded).not.toBe(chevron);
});

test("in the macOS app the title bar alone drags the window, clears the traffic lights and holds the toggle, which does not drag, folded or not", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mount("#/runs");
  for (const fold of [false, true]) {
    if (fold) fireEvent.click(toggle());
    for (const name of ["h-[30px]", "pl-[76px]", DRAG]) expect(titleBar().classList.contains(name)).toBe(true);
    expect(childNames(titleBar())).toEqual([fold ? "Expand sidebar" : "Collapse sidebar", "holophyte", "Back", "Next"]);
    for (const control of [toggle(), homeLink(), ...within(titleBar()).getAllByRole("button", { name: /^(Back|Next)$/ })]) {
      expect(control.classList.contains(NO_DRAG)).toBe(true);
    }
    const regions = Array.from(document.querySelectorAll<HTMLElement>("[class]")).filter((el) => el.classList.contains(DRAG));
    expect(regions).toEqual([titleBar()]);
    expect(screen.getByRole("main").querySelector('[aria-hidden="true"].h-0')).toBeNull();
  }
  expect(aside().classList.contains("min-[720px]:w-[56px]")).toBe(true);
});

test("in the macOS app the Settings menu keeps the drag title bar with no toggle, the home link and Back and Next in it", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mount("#/settings/general");
  expect(screen.queryByRole("button", { name: /^(Collapse|Expand) sidebar$/ })).toBeNull();
  expect(titleBar().classList.contains(DRAG)).toBe(true);
  expect(childNames(titleBar())).toEqual(["holophyte", "Back", "Next"]);
});

const historyButton = (name: "Back" | "Next") => within(titleBar()).getByRole<HTMLButtonElement>("button", { name });
const page = () => [window.location.hash.split("?")[0], pages().filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.textContent)];
const OVERVIEW = ["#/", ["Overview"]];
const TICKETS = ["#/tickets", ["Tickets"]];
const openTickets = async () => {
  fireEvent.click(pages().find((link) => link.textContent?.startsWith("Tickets"))!);
  await act(settle);
};
const mountAt = async (hash: string) => {
  happyDOM.setURL(`${ORIGIN}/`);
  window.history.replaceState(null, "");
  sessionStorage.clear();
  await mount(hash);
};
const settleHistory = async () => {
  await act(settle);
  await act(settle);
};

test("in the macOS app Back and Next follow the wordmark, disabled with nowhere to go, and step through the console's own navigation", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mountAt("#/");
  expect(childNames(titleBar())).toEqual(["Collapse sidebar", "holophyte", "Back", "Next"]);
  expect(historyButton("Back").disabled).toBe(true);
  expect(historyButton("Next").disabled).toBe(true);

  await openTickets();
  expect(page()).toEqual(TICKETS);
  expect(historyButton("Back").disabled).toBe(false);
  expect(historyButton("Next").disabled).toBe(true);

  fireEvent.click(historyButton("Back"));
  await settleHistory();
  expect(page()).toEqual(OVERVIEW);
  expect(historyButton("Back").disabled).toBe(true);
  expect(historyButton("Next").disabled).toBe(false);

  fireEvent.click(historyButton("Next"));
  await settleHistory();
  expect(page()).toEqual(TICKETS);
  expect(historyButton("Next").disabled).toBe(true);
});

test("in the macOS app, after going Back, a reload of the same window keeps Next enabled and it still returns to Tickets", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mountAt("#/");
  await openTickets();
  fireEvent.click(historyButton("Back"));
  await settleHistory();
  cleanup();

  await mount("#/");
  expect(page()).toEqual(OVERVIEW);
  expect(historyButton("Next").disabled).toBe(false);
  fireEvent.click(historyButton("Next"));
  await settleHistory();
  expect(page()).toEqual(TICKETS);
});

test("in the macOS app a reload that resets the entry's history state leaves Next disabled", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mountAt("#/");
  await openTickets();
  fireEvent.click(historyButton("Back"));
  await settleHistory();
  cleanup();

  window.history.replaceState(null, "");
  await mount("#/");
  expect(page()).toEqual(OVERVIEW);
  expect(historyButton("Next").disabled).toBe(true);
});

test("in the macOS app Back disables when the window's history length leaves no entry behind the current one", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mountAt("#/");
  await openTickets();
  fireEvent.click(pages().find((link) => link.textContent?.startsWith("Runs"))!);
  await act(settle);
  Object.defineProperty(window.history, "length", { configurable: true, get: () => 2 });
  try {
    fireEvent.click(historyButton("Back"));
    await settleHistory();
    expect(page()).toEqual(TICKETS);
    expect(historyButton("Back").disabled).toBe(true);
    expect(historyButton("Next").disabled).toBe(false);
  } finally {
    delete (window.history as { length?: number }).length;
  }
});

test("in the macOS app Cmd with [ goes back and Cmd with ] goes forward", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mountAt("#/");
  await openTickets();
  fireEvent.keyDown(document, { key: "[", metaKey: true });
  await settleHistory();
  expect(page()).toEqual(OVERVIEW);
  fireEvent.keyDown(document, { key: "]", metaKey: true });
  await settleHistory();
  expect(page()).toEqual(TICKETS);
});

test("in a browser the title bar holds no Back or Next", async () => {
  await mountAt("#/");
  await openTickets();
  expect(childNames(titleBar())).toEqual(["Collapse sidebar", "holophyte"]);
});

test("in a browser nothing names a window drag region", async () => {
  await mount("#/runs");
  const named = Array.from(document.querySelectorAll<HTMLElement>("[class]")).filter((el) => el.className.includes("-webkit-app-region"));
  expect(named).toEqual([]);
  expect(screen.getByRole("main").firstElementChild!.getAttribute("aria-hidden")).not.toBe("true");
});

test("the haze band is drawn inside the page panel from its top edge, not in the title bar", async () => {
  await mount("#/");
  const main = screen.getByRole("main");
  const band = document.querySelector<HTMLElement>("[data-haze]")!;
  expect(band.parentElement).toBe(main);
  for (const name of ["absolute", "top-0", "inset-x-0"]) expect(band.classList.contains(name)).toBe(true);
  for (const name of ["relative", "isolate"]) expect(main.classList.contains(name)).toBe(true);
  expect(titleBar().contains(band)).toBe(false);
  expect(titleBar().compareDocumentPosition(main) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("without WebGL2 the shell's haze band shows the fallback, hidden from assistive technology and taking no pointer events", async () => {
  const getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof getContext;
  localStorage.setItem(THEME_KEY, "dark");
  const style = document.createElement("style");
  style.textContent = (await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text()).replace(/@import[^;]*;/, "");
  document.head.appendChild(style);
  try {
    await mount("#/");
    const bands = screen.getByRole("main").querySelectorAll<HTMLElement>("[data-haze]");
    expect(bands.length).toBe(1);
    const band = bands[0]!;
    expect(band.classList.contains("haze-fallback")).toBe(true);
    expect(band.querySelector("canvas")).toBeNull();
    expect(band.getAttribute("aria-hidden")).toBe("true");
    expect(getComputedStyle(band).pointerEvents).toBe("none");
  } finally {
    HTMLCanvasElement.prototype.getContext = getContext;
    style.remove();
  }
});

test("a WebGL2 context whose shaders fail to compile leaves the shell up with the haze band on its fallback", async () => {
  const getContext = HTMLCanvasElement.prototype.getContext;
  const failingGl = new Proxy({}, {
    get: (_, name) => (name === "getExtension"
      ? (extension: string) => (extension === "EXT_color_buffer_float" ? {} : null)
      : name === "getShaderParameter" ? () => false : () => ({})),
  });
  HTMLCanvasElement.prototype.getContext = ((kind: string) => (kind === "webgl2" ? failingGl : null)) as typeof getContext;
  localStorage.setItem(THEME_KEY, "dark");
  try {
    await mount("#/");
    expect(screen.getByRole("navigation", { name: "Pages" })).toBeTruthy();
    const band = screen.getByRole("main").querySelector<HTMLElement>("[data-haze]")!;
    expect(band.classList.contains("haze-fallback")).toBe(true);
    expect(band.querySelector("canvas")).toBeNull();
  } finally {
    HTMLCanvasElement.prototype.getContext = getContext;
  }
});

test("a canvas whose getContext throws leaves the shell up with the haze band on its fallback and no canvas left behind", async () => {
  const getContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = (() => { throw new Error("context creation blocked"); }) as typeof getContext;
  localStorage.setItem(THEME_KEY, "dark");
  try {
    await mount("#/");
    expect(screen.getByRole("navigation", { name: "Pages" })).toBeTruthy();
    const band = screen.getByRole("main").querySelector<HTMLElement>("[data-haze]")!;
    expect(band.classList.contains("haze-fallback")).toBe(true);
    expect(band.querySelector("canvas")).toBeNull();
  } finally {
    HTMLCanvasElement.prototype.getContext = getContext;
  }
});

test("a stored header effect of none renders no haze band, and with nothing stored the shell renders one", async () => {
  localStorage.setItem("holophyte.next.effect", "none");
  await mount("#/");
  expect(screen.getByRole("main").querySelector("[data-haze]")).toBeNull();
  cleanup();
  localStorage.clear();
  await mount("#/");
  expect(screen.getByRole("main").querySelectorAll("[data-haze]").length).toBe(1);
});

test("Cmd or Ctrl with backslash folds and unfolds the sidebar, and other chords leave it", async () => {
  await mount("#/");
  fireEvent.keyDown(document, { key: "\\", metaKey: true });
  expect(screen.getByRole("button", { name: "Expand sidebar" }).getAttribute("aria-expanded")).toBe("false");
  cleanup();

  await mount("#/");
  expect(screen.getByRole("button", { name: "Expand sidebar" })).toBeTruthy();
  fireEvent.keyDown(document, { key: "\\", ctrlKey: true });
  expect(screen.getByRole("button", { name: "Collapse sidebar" }).getAttribute("aria-expanded")).toBe("true");
  for (const chord of [{}, { metaKey: true, shiftKey: true }]) {
    fireEvent.keyDown(document, { key: "\\", ...chord });
    expect(screen.getByRole("button", { name: "Collapse sidebar" }).getAttribute("aria-expanded")).toBe("true");
  }
});

test("the sidebar toggle's tooltip names its action and the platform's fold chord on hover or keyboard focus", async () => {
  const native = /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent) ? "apple" : "other";
  try {
    for (const [platform, chord] of [["apple", "⌘\\"], ["other", "Ctrl+\\"]] as const) {
      setPlatform(platform);
      await mount("#/");
      expect(tooltip()).toBeNull();
      await pointerOver(toggle());
      expect(tooltip()!.textContent).toBe(`Collapse sidebar\u2066${chord}\u2069`);
      await pointerAway(toggle());
      fireEvent.click(toggle());
      expect(tooltip()).toBeNull();
      await keyboardFocus(toggle());
      expect(tooltip()!.textContent).toBe(`Expand sidebar\u2066${chord}\u2069`);
      cleanup();
      localStorage.clear();
    }
  } finally {
    setPlatform(native);
  }
});

test("folded, a rail link's tooltip names its page and count on keyboard focus or hover, describes the link, and leaves on blur or pointer leave", async () => {
  localStorage.setItem("holophyte.next.rail", "folded");
  await mount("#/", needsDaemons());
  const overview = screen.getByRole("link", { name: "Overview, 2 waiting on you" });
  expect(tooltip()).toBeNull();

  await keyboardFocus(overview);
  expect(tooltip()!.textContent).toBe("Overview 2");
  expect(overview.getAttribute("aria-describedby")).toBe(tooltip()!.id);
  await act(async () => overview.blur());
  expect(tooltip()).toBeNull();

  await pointerOver(overview);
  expect(tooltip()!.textContent).toBe("Overview 2");
  await pointerAway(overview);
  expect(tooltip()).toBeNull();

  const runs = screen.getByRole("link", { name: "Runs" });
  await pointerOver(runs);
  expect(tooltip()!.textContent).toBe("Runs");
  expect(runs.getAttribute("aria-describedby")).toBe(tooltip()!.id);
});

test("folding and unfolding with Cmd+backslash keeps keyboard focus on the focused page link", async () => {
  await mount("#/");
  const runs = screen.getByRole("link", { name: "Runs" });
  await keyboardFocus(runs);
  for (const _press of [1, 2]) {
    fireEvent.keyDown(document, { key: "\\", metaKey: true });
    expect(document.activeElement).toBe(runs);
  }
});

test("expanded, no page link carries a tooltip on focus or hover, and the current page's link is marked current", async () => {
  await mount("#/findings", needsDaemons());
  const findings = screen.getByRole("link", { name: /^Findings/ });
  expect(findings.getAttribute("aria-current")).toBe("page");
  await keyboardFocus(findings);
  await pointerOver(findings);
  expect(tooltip()).toBeNull();
  expect(pages().filter((link) => link.hasAttribute("aria-describedby"))).toEqual([]);
});

test("each page's heading section opens on the title row's padding", async () => {
  for (const hash of ["#/runs", "#/tickets", "#/findings", "#/settings/general"]) {
    await mount(hash);
    const section = document.getElementById("page-title")!.closest('section[aria-labelledby="page-title"]')!;
    expect(section.classList.contains("pt-3.5")).toBe(true);
    expect(section.classList.contains("py-7")).toBe(false);
    cleanup();
  }
});

test("off Settings the sidebar keeps the project switcher and the Pages nav and holds no Settings sections", async () => {
  for (const hash of ["#/", "#/runs?project=demo", "#/tickets", "#/findings", "#/scouts", "#/stories"]) {
    await mount(hash);
    const sidebar = within(screen.getByRole("complementary", { name: "Console" }));
    expect(sidebar.getByRole("button", { name: /Switch project$/ })).toBeTruthy();
    expect(sidebar.getByRole("navigation", { name: "Pages" })).toBeTruthy();
    expect(sidebar.queryByRole("navigation", { name: "Settings sections" })).toBeNull();
    cleanup();
  }
});

const NOV_6 = Date.UTC(2026, 10, 6);
const DAY = 24 * 3600 * 1000;
const WEEK = 7 * DAY;
const NO_WAITS = { pending_checks: 0, checks_failing: 0, thread: 0, rounds_used: 0, behind_main: 0, main_red: 0, approval: 0, other: 0 };

function flowOf(weeks: { merged: number; working: number; waiting: number; operator: number }[], waits = NO_WAITS): Flow {
  return {
    now: NOV_6 + WEEK,
    buckets: weeks.map(({ merged, working, waiting, operator }, index) => ({
      start_ms: NOV_6 - (weeks.length - 1 - index) * WEEK,
      merged,
      rounds_median: 2,
      interventions: 1,
      hours: { working, waiting, operator },
    })),
    phases: { working: 1 },
    waits,
    reverted: { count: 0, merged: 3 },
  };
}

const demoFlow = flowOf([{ merged: 3, working: 4, waiting: 12, operator: 1 }, { merged: 4, working: 6, waiting: 8, operator: 4 }]);
const writerFlow = flowOf([{ merged: 5, working: 30, waiting: 10, operator: 0 }]);

function flowDaemons(demo: Flow, writer: Flow): Fetch {
  const polls = daemons([], []);
  return async (url, init) => {
    if (url.startsWith(`${ORIGIN}/flow?`)) return Response.json(demo);
    if (url.startsWith(`${PEER}/flow?`)) return Response.json(writer);
    return polls(url, init);
  };
}

async function flush() {
  for (let round = 0; round < 6; round += 1) await act(settle);
}

const figure = (label: string) => document.querySelector(`[data-number="${label}"]`)!.textContent;
const projectPicker = () => screen.getByRole("button", { name: "Project: All projects" });

test("the sidebar's Flow link opens the Flow page", async () => {
  happyDOM.setURL(`${ORIGIN}/`);
  await mount("#/", flowDaemons(demoFlow, writerFlow));
  const link = screen.getByRole("link", { name: "Flow" });
  expect(link.getAttribute("href")).toMatch(/#\/flow(\?|$)/);
  fireEvent.click(link);
  await flush();
  expect(window.location.hash).toMatch(/^#\/flow/);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Flow");
});

test("the Flow page header holds the one Flow title, the window's span beside it, and the Project and Range tools", async () => {
  await mount("#/flow", flowDaemons(demoFlow, writerFlow));
  await act(settle);
  const [title, ...more] = screen.getAllByRole("heading", { level: 1 });
  expect(more).toHaveLength(0);
  expect(title!.textContent).toBe("Flow");
  const [first, last] = [new Date(NOV_6 - WEEK), new Date(NOV_6 + WEEK - 1)];
  expect(title!.nextElementSibling!.textContent).toBe(`14 days, ${first.getDate()} October to ${last.getDate()} November`);
  const header = title!.closest("header")!;
  expect(header.contains(projectPicker())).toBe(true);
  expect(header.contains(screen.getByRole("button", { name: "Range" }))).toBe(true);
});

test("on All projects the Flow numbers sum both hosts: merged adds, and waiting is waiting and operator hours over all hours", async () => {
  await mount("#/flow", flowDaemons(demoFlow, writerFlow));
  await act(settle);
  expect(projectPicker().textContent).toBe("All projects");
  const numbers = document.querySelector('dl[aria-label="Flow numbers"]')!;
  expect([...numbers.querySelectorAll("dt")].map((term) => term.textContent)).toEqual(["Merged", "Waiting", "Rounds per merge", "Interventions per merge", "Reverted"]);
  expect([...numbers.querySelectorAll("[data-number]")].map((number) => number.getAttribute("data-number"))).toEqual(["Merged", "Waiting", "Rounds per merge", "Interventions per merge", "Reverted"]);
  expect(figure("Merged")).toBe("12");
  expect(figure("Waiting")).toBe("47%");
  expect(figure("Reverted")).toBe("0 of 6");
});

test("the Reverted figure says it counts the page's merges reverted within 14 days", async () => {
  await mount("#/flow", flowDaemons(demoFlow, writerFlow));
  await act(settle);
  const reverted = screen.getByText("Reverted", { selector: "dt" }).parentElement!;
  expect(within(reverted).getByText("of these merges, within 14 days")).toBeTruthy();
  expect(reverted.textContent).not.toContain("30 days");
});

test("on All projects a host whose revert count is unknown leaves the summed Reverted unknown", async () => {
  await mount("#/flow", flowDaemons(demoFlow, { ...writerFlow, reverted: null }));
  await act(settle);
  expect(figure("Merged")).toBe("12");
  expect(figure("Reverted")).toBe("–");
});

test("the Flow Project picker lists All projects then each project, and picking one shows only its host's numbers and puts it in the address", async () => {
  happyDOM.setURL(`${ORIGIN}/`);
  await mount("#/flow", flowDaemons(demoFlow, writerFlow));
  await act(settle);
  expect(document.querySelector("select")).toBeNull();
  fireEvent.click(projectPicker());
  const options = within(screen.getByRole("listbox", { name: "Project" })).getAllByRole("option");
  expect(options.map((option) => option.textContent)).toEqual(["All projects", "demo", "writer"]);
  expect(options.map((option) => option.getAttribute("aria-selected"))).toEqual(["true", "false", "false"]);
  fireEvent.click(options[2]!);
  await flush();
  expect(new URLSearchParams(window.location.hash.split("?")[1]).get("project")).toBe("writer");
  expect(figure("Merged")).toBe("5");
  expect(figure("Waiting")).toBe("25%");
  expect(figure("Reverted")).toBe("0 of 3");
});

test("Biggest waits leads with the largest group, and approval carries the by design tag", async () => {
  const waits = { ...NO_WAITS, approval: 40, pending_checks: 3, thread: 12 };
  await mount("#/flow?project=demo", flowDaemons(flowOf([{ merged: 1, working: 1, waiting: 55, operator: 0 }], waits), writerFlow));
  await act(settle);
  const rows = within(screen.getByRole("list", { name: "Biggest waits" })).getAllByRole("listitem");
  expect(rows.map((row) => row.getAttribute("data-wait"))).toEqual(["approval", "thread", "pending_checks"]);
  expect(within(rows[0]!).getByText("by design")).toBeTruthy();
  expect(rows.slice(1).some((row) => row.textContent!.includes("by design"))).toBe(false);
});

function askedFlows(asked: URL[]): Fetch {
  const polls = daemons([], []);
  return async (url, init) => {
    if (!/\/flow\?/.test(url)) return polls(url, init);
    const query = new URL(url);
    asked.push(query);
    const days = Number(query.searchParams.get("days"));
    const size = query.searchParams.get("bucket") === "week" ? WEEK : DAY;
    const count = Math.ceil((days * DAY) / size);
    const flow = flowOf(Array.from({ length: count }, () => ({ merged: 1, working: 2, waiting: 1, operator: 0 })));
    const until = query.searchParams.get("until");
    if (until == null) return Response.json(flow);
    const end = Number(until);
    return Response.json({ ...flow, now: end, buckets: flow.buckets!.map((row, index) => ({ ...row, start_ms: Math.max(end - days * DAY, end - (count - index) * size) })) });
  };
}

test("opened with range=90d the Flow page asks each host for ninety days by week and every chart shows a point a week", async () => {
  const asked: URL[] = [];
  await mount("#/flow?range=90d", askedFlows(asked));
  await flush();
  expect(new Set(asked.map((url) => url.origin))).toEqual(new Set([ORIGIN, PEER]));
  expect(asked.every((url) => url.searchParams.get("days") === "90" && url.searchParams.get("bucket") === "week" && !url.searchParams.has("until"))).toBe(true);
  const charts = [...document.querySelectorAll("figure[data-chart]")];
  expect(charts).toHaveLength(4);
  expect(charts.map((chart) => chart.querySelectorAll("[data-point]").length)).toEqual([13, 13, 13, 13]);
});

test("the open Range menu marks the current preset, and choosing From and To puts them in the address and asks for the whole days ending with the To day", async () => {
  happyDOM.setURL(`${ORIGIN}/`);
  const asked: URL[] = [];
  await mount("#/flow?range=90d", askedFlows(asked));
  await flush();
  fireEvent.click(screen.getByRole("button", { name: "Range" }));
  const choices = within(screen.getByRole("group", { name: "Range choices" }));
  expect(choices.getAllByRole("button", { pressed: true }).map((choice) => choice.textContent)).toEqual(["Last 90 days"]);
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-09-03" } });
  fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-11-12" } });
  await flush();
  const search = new URLSearchParams(window.location.hash.split("?")[1]);
  expect([search.get("from"), search.get("to")]).toEqual(["2026-09-03", "2026-11-12"]);
  const ends = asked.filter((url) => url.searchParams.has("until"));
  expect(new Set(ends.map((url) => url.origin))).toEqual(new Set([ORIGIN, PEER]));
  const last = ends.at(-1)!.searchParams;
  expect([last.get("days"), last.get("bucket"), last.get("until")]).toEqual(["71", "week", String(new Date(2026, 10, 13).getTime())]);
  expect(screen.getByRole("heading", { level: 1 }).nextElementSibling!.textContent).toEndWith(" to 12 November");
});

test("the open Range menu closes when focus moves out of it to the Project picker", async () => {
  await mount("#/flow", flowDaemons(demoFlow, writerFlow));
  await act(settle);
  const range = screen.getByRole("button", { name: "Range" });
  range.focus();
  fireEvent.click(range);
  screen.getByLabelText("From").focus();
  expect(screen.getByRole("group", { name: "Range choices" })).toBeTruthy();
  act(() => projectPicker().focus());
  expect(screen.queryByRole("group", { name: "Range choices" })).toBeNull();
});

test("a From and To over 182 days, or with a To day not yet ended, stay out of the address and the asks", async () => {
  happyDOM.setURL(`${ORIGIN}/`);
  const later = new Date(Date.now() + 2 * DAY);
  const future = `${later.getFullYear()}-${String(later.getMonth() + 1).padStart(2, "0")}-${String(later.getDate()).padStart(2, "0")}`;
  for (const [from, to] of [["2026-01-01", "2026-07-02"], ["2026-11-01", future]]) {
    const asked: URL[] = [];
    await mount("#/flow", askedFlows(asked));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Range" }));
    fireEvent.change(screen.getByLabelText("From"), { target: { value: from } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: to } });
    await flush();
    const search = new URLSearchParams(window.location.hash.split("?")[1]);
    expect([search.get("from"), search.get("to")]).toEqual([null, null]);
    expect(asked.every((url) => url.searchParams.get("days") === "30" && !url.searchParams.has("until"))).toBe(true);
    cleanup();
  }
});

const SEARCH_ANSWER = {
  q: "witness",
  limit: 20,
  tickets: [{ ticket: "DEMO-69", title: "Witness turn runs the real tool once", status: "merged" }],
  runs: [
    { id: 403, ticket: "DEMO-69", title: "Witness turn runs the real tool once", phase: "done", outcome: "merged", started_ms: 1 },
    { id: 411, ticket: "DEMO-74", title: "Witness criterion for git tickets", phase: "implementing", outcome: null, started_ms: 2 },
  ],
  scouts: [{ id: 3, title: "Witness coverage across projects", phase: "swarming" }],
  findings: [{ run: 411, round: 2, severity: "p1", message: "witness turn skipped on the merge gate", path: "holophyte/loop/gates.py", line: 212 }],
};

function searchDaemons(posts: string[] = []): Fetch {
  const polls = daemons([], []);
  return async (url, init) => {
    if (init?.method === "POST") {
      posts.push(url);
      return Response.json({ ok: true, detail: "done" });
    }
    if (url.startsWith(`${ORIGIN}/search?`)) return Response.json({ ...SEARCH_ANSWER, q: new URL(url).searchParams.get("q") });
    return polls(url, init);
  };
}

const paletteKey = (init: { metaKey?: boolean; ctrlKey?: boolean } = { metaKey: true }) =>
  act(async () => void fireEvent.keyDown(document.activeElement ?? document.body, { key: "k", ...init }));
const palette = () => screen.queryByRole("dialog", { name: "Command palette" });
const paletteInput = () => within(palette()!).getByRole("combobox");
const groupNames = () => within(palette()!).queryAllByRole("group").map((group) => group.getAttribute("aria-labelledby") && document.getElementById(group.getAttribute("aria-labelledby")!)!.textContent);
const optionsOf = (name: string) => within(within(palette()!).getByRole("group", { name })).getAllByRole("option");
const titlesOf = (name: string) => optionsOf(name).map((option) => option.querySelector("[data-title]")!.textContent);

async function typeIn(value: string) {
  await act(async () => void fireEvent.input(paletteInput(), { target: { value } }));
  await act(() => new Promise((resolve) => setTimeout(resolve, 200)));
  await flush();
}

async function paletteKeyDown(key: string) {
  await act(async () => void fireEvent.keyDown(paletteInput(), { key }));
  await flush();
}

test("⌘K or Ctrl+K opens the Command palette with its input focused, and Escape closes it back to the focused element", async () => {
  await mount("#/runs", searchDaemons());
  for (const init of [{ metaKey: true }, { ctrlKey: true }]) {
    const before = screen.getByRole("link", { name: "Tickets" });
    before.focus();
    await paletteKey(init);
    await flush();
    expect(palette()).toBeTruthy();
    expect(document.activeElement).toBe(paletteInput());
    await act(async () => void fireEvent.keyDown(paletteInput(), { key: "Escape" }));
    await flush();
    expect(palette()).toBeNull();
    expect(document.activeElement).toBe(before);
  }
});

test("an empty palette reads Recent, newest first, then Jump to and Switch project", async () => {
  happyDOM.setURL(`${ORIGIN}/`);
  await mount("#/", searchDaemons());
  for (const page of ["Runs", "Findings"]) {
    await paletteKey();
    await flush();
    const jump = optionsOf("Jump to");
    expect(titlesOf("Jump to")).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts", "Flow", "Appearance"]);
    fireEvent.click(jump[titlesOf("Jump to").indexOf(page)]!);
    await flush();
    expect(palette()).toBeNull();
  }
  expect(window.location.hash.split("?")[0]).toBe("#/findings");
  await paletteKey();
  await flush();
  expect(groupNames()).toEqual(["Recent", "Jump to", "Switch project"]);
  expect(titlesOf("Recent")).toEqual(["Findings", "Runs"]);
  expect(titlesOf("Switch project")).toEqual(["All projects", "demo", "writer"]);
});

test("typing a search lists Tickets, Runs, Scouts, Findings, Pages and Actions in that order, with the matches marked", async () => {
  await mount("#/", searchDaemons());
  await paletteKey();
  await typeIn("witness");
  expect(groupNames()).toEqual(["Tickets", "Runs", "Scouts", "Findings", "Pages", "Actions"]);
  expect(optionsOf("Runs")).toHaveLength(2);
  expect(optionsOf("Tickets")[0]!.querySelector("mark")!.textContent).toBe("Witness");
  expect(titlesOf("Actions")).toContain('New ticket "witness"');
});

test("a scope character becomes a chip that narrows the list, and Backspace on the empty input removes it", async () => {
  await mount("#/", searchDaemons());
  await paletteKey();
  await typeIn(">");
  const chip = () => palette()!.querySelector("[data-chip]");
  expect(chip()!.textContent).toBe("Actions");
  expect((paletteInput() as HTMLInputElement).value).toBe("");
  expect(groupNames()).toEqual(["Actions"]);
  expect(titlesOf("Actions")).toEqual(["New ticket", "Launch scout", "Switch theme", "Run sweep"]);
  await paletteKeyDown("Backspace");
  expect(chip()).toBeNull();
  expect(groupNames()).toEqual(["Jump to", "Switch project"]);

  await typeIn("#witness");
  expect(chip()!.textContent).toBe("Tickets");
  expect(groupNames()).toEqual(["Tickets"]);
  await typeIn("");
  await paletteKeyDown("Backspace");
  await typeIn("@witness");
  expect(chip()!.textContent).toBe("Runs");
  expect(groupNames()).toEqual(["Runs"]);
});

test("↓ twice then Enter opens that run's page and puts it first in Recent", async () => {
  happyDOM.setURL(`${ORIGIN}/`);
  await mount("#/", searchDaemons());
  await paletteKey();
  await typeIn("witness");
  await paletteKeyDown("ArrowDown");
  await paletteKeyDown("ArrowDown");
  await paletteKeyDown("Enter");
  expect(palette()).toBeNull();
  expect(window.location.hash.split("?")[0]).toBe("#/runs/demo/411");
  await paletteKey();
  await flush();
  expect(titlesOf("Recent")[0]).toBe("run 411 · DEMO-74 Witness criterion for git tickets");
});

test("Run sweep asks for an inline confirm naming its host, with Confirm only on the button: Cancel returns to the list, Confirm posts once to the sweep route", async () => {
  const posts: string[] = [];
  const polls = searchDaemons(posts);
  const host = (await Bun.file(new URL("host-status.json", SERVE)).json()) as Status;
  await mount("#/", async (url, init) => (url === `${ORIGIN}/status` ? Response.json({ ...host, projects: [], actions: true }) : polls(url, init)));
  await paletteKey();
  await typeIn(">sweep");
  fireEvent.click(optionsOf("Actions")[0]!);
  await flush();
  expect(within(palette()!).queryByRole("listbox")).toBeNull();
  const confirm = within(palette()!).getByRole("region", { name: "Run sweep" });
  expect(within(confirm).getByRole("heading", { name: "Run sweep" })).toBeTruthy();
  expect(within(confirm).getByText("on writer-host")).toBeTruthy();
  expect(within(confirm).getAllByRole("button").map((button) => button.textContent)).toEqual(["Cancel", "Confirm"]);
  expect(within(confirm).getAllByText("Confirm", { exact: false }).map((node) => node.closest("button")?.textContent ?? null)).toEqual(["Confirm"]);
  expect(posts).toEqual([]);
  fireEvent.click(within(palette()!).getByRole("button", { name: "Cancel" }));
  await flush();
  expect(groupNames()).toEqual(["Actions"]);
  expect(posts).toEqual([]);
  fireEvent.click(optionsOf("Actions")[0]!);
  await flush();
  await act(async () => void fireEvent.click(within(palette()!).getByRole("button", { name: "Confirm" })));
  await flush();
  expect(posts).toEqual([`${ORIGIN}/actions/run-sweep`]);
  expect(palette()).toBeNull();
});

test("a query that is exactly a ticket key puts that ticket first under Go to, and the runs chip lists only runs", async () => {
  await mount("#/", searchDaemons());
  await paletteKey();
  await typeIn("demo-69");
  expect(groupNames()).toEqual(["Go to", "Runs", "Scouts", "Findings", "Pages", "Actions"]);
  expect(titlesOf("Go to")).toEqual(["DEMO-69 Witness turn runs the real tool once"]);
  await typeIn("");
  await typeIn("@DEMO-69");
  expect(groupNames()).toEqual(["Runs"]);
});

test("Recent keeps the items opened this page while storage refuses writes", async () => {
  happyDOM.setURL(`${ORIGIN}/`);
  await mount("#/", searchDaemons());
  const write = Storage.prototype.setItem;
  Storage.prototype.setItem = () => {
    throw new DOMException("full", "QuotaExceededError");
  };
  try {
    for (const page of ["Runs", "Findings"]) {
      await paletteKey();
      await flush();
      fireEvent.click(optionsOf("Jump to")[titlesOf("Jump to").indexOf(page)]!);
      await flush();
    }
    await paletteKey();
    await flush();
    expect(titlesOf("Recent")).toEqual(["Findings", "Runs"]);
  } finally {
    Storage.prototype.setItem = write;
  }
});

test("New ticket shows disabled with the actions-off reason while no daemon in scope takes tickets", async () => {
  await mount("#/", searchDaemons());
  await paletteKey();
  await typeIn(">new");
  const option = optionsOf("Actions")[0]!;
  expect(option.getAttribute("aria-disabled")).toBe("true");
  expect(option.getAttribute("title")).toBe(ACTIONS_OFF);
  const hash = window.location.hash;
  fireEvent.click(option);
  await flush();
  expect(palette()).toBeTruthy();
  expect(window.location.hash).toBe(hash);
});

test("Confirm sweeps only the daemons its confirm named, though another answers by then", async () => {
  const posts: string[] = [];
  const host = (await Bun.file(new URL("host-status.json", SERVE)).json()) as Status;
  let peerUp = false;
  const polls = searchDaemons(posts);
  window.location.hash = "#/";
  const client = consoleQueryClient();
  const fetchImpl: Fetch = async (url, init) => {
    if (init?.method !== "POST" && url === `${ORIGIN}/status`) return Response.json({ ...host, projects: [], actions: true });
    if (init?.method !== "POST" && url.startsWith(`${PEER}/`) && !peerUp) throw new Error("connection refused");
    if (init?.method !== "POST" && url === `${PEER}/status`) return Response.json({ ...host, host: "spare", projects: [], actions: true });
    return polls(url, init);
  };
  render(<QueryClientProvider client={client}><ConsoleApp base={ORIGIN} pollDeps={fakeDeps(fetchImpl).deps} /></QueryClientProvider>);
  await flush();
  await paletteKey();
  await typeIn(">sweep");
  fireEvent.click(optionsOf("Actions")[0]!);
  await flush();
  expect(within(palette()!).getByText("on writer-host")).toBeTruthy();

  peerUp = true;
  await act(async () => void (await client.refetchQueries({ queryKey: ["fleet", ORIGIN] })));
  await flush();
  await act(async () => void fireEvent.click(within(palette()!).getByRole("button", { name: "Confirm" })));
  await flush();
  expect(posts).toEqual([`${ORIGIN}/actions/run-sweep`]);
});
