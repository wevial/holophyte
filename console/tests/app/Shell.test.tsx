import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { THEME_KEY } from "../../src/lib/theme";
import { APP_SHELL_KEY } from "../../src/lib/view";
import { TICK_MS, type Fetch } from "../../src/lib/poll";
import type { Attention, Board, Status } from "../../src/lib/contract.gen";
import { fakeDeps, peersFetch, settle } from "../harness";
import { needsDaemons } from "./needsFleet";
import { declarations } from "../cssTokens";

const ORIGIN = "http://writer-host:7710";
const PEER = "http://writer-host:7711";

const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const pinned = (await Bun.file(new URL("board.json", SERVE)).json()) as Board;

const happyDOM = (window as unknown as {
  happyDOM: { settings: { device: { prefersColorScheme: string } }; setViewport: (viewport: { width: number }) => void };
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
  window.location.hash = "";
  device.prefersColorScheme = "light";
});

test("a scoped hash opens its page, marks its link current and keeps the scope on every page link", async () => {
  await mount("#/runs?project=demo");
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Runs");
  const links = pages();
  expect(links.map((link) => link.getAttribute("aria-label"))).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts"]);
  expect(links.filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.textContent)).toEqual(["Runs"]);
  for (const link of links) {
    expect(new URLSearchParams(link.getAttribute("href")!.split("?")[1]).get("project")).toBe("demo");
  }
});

test("the Overview badge counts the scope's Waiting on you entries, as the heading does, and hides at zero", async () => {
  await mount("#/", needsDaemons());
  expect(screen.getByRole("link", { name: "Overview, 2 waiting on you" })).toBeTruthy();
  expect(badge()!.textContent).toBe("2");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("2 waiting on you.");
  cleanup();

  await mount("#/?project=demo", needsDaemons());
  expect(screen.getByRole("link", { name: "Overview, 2 waiting on you" })).toBeTruthy();
  expect(badge()!.textContent).toBe("2");
  cleanup();

  await mount("#/");
  expect(screen.getByRole("link", { name: "Overview" })).toBeTruthy();
  expect(badge()).toBeNull();
});

test("an unknown route links back to Overview", async () => {
  await mount("#/nowhere");
  expect(await screen.findByText("There is no page here.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Go to Overview" }).getAttribute("href")).toMatch(/#\/$/);
});

test("the sidebar sits on its own ground with a rule beside the main area, which keeps the page's ground", async () => {
  await mount("#/");
  const sidebar = screen.getByRole("complementary", { name: "Console" }).classList;
  expect(sidebar.contains("bg-side")).toBe(true);
  expect(sidebar.contains("border-r")).toBe(true);
  expect(sidebar.contains("border-rule")).toBe(true);
  expect(screen.getByRole("main").classList.contains("bg-side")).toBe(false);
});

test("the sidebar footer, folded or not, holds Settings and the theme toggle and no Classic console link", async () => {
  await mount("#/");
  for (const fold of [false, true]) {
    if (fold) fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    const footer = screen.getByRole("complementary", { name: "Console" }).querySelector("footer")!;
    expect(within(footer).getByRole("link", { name: "Settings" })).toBeTruthy();
    expect(within(footer).getByRole("group", { name: "Theme" })).toBeTruthy();
    expect(within(footer).queryByRole("button", { name: /colour theme|switch to/i })).toBeNull();
    if (!fold) {
      const row = within(footer).getByRole("link", { name: "Settings" }).parentElement!;
      expect(row.parentElement).toBe(footer);
      expect(row.previousElementSibling).toBe(footer.querySelector("p"));
      expect([...row.children]).toEqual([within(footer).getByRole("link", { name: "Settings" }), within(footer).getByRole("group", { name: "Theme" })]);
    }
    expect(screen.queryByRole("link", { name: /classic/i })).toBeNull();
    expect(document.querySelector('a[href*="classic"]')).toBeNull();
  }
});

test("the fold button folds the sidebar to icons, keeps link names and survives a remount and failing storage", async () => {
  await mount("#/");
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  const expand = screen.getByRole("button", { name: "Expand sidebar" });
  expect(expand.getAttribute("aria-expanded")).toBe("false");
  expect(pages().map((link) => link.textContent)).toEqual(["", "", "", "", ""]);
  expect(pages().map((link) => link.getAttribute("aria-label"))).toEqual(["Overview", "Runs", "Tickets", "Findings", "Scouts"]);
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

test("the footer names the host and how long ago it was polled, and reads polling before any answer", async () => {
  const pending: Fetch = () => new Promise(() => {});
  await mount("#/", pending);
  expect(screen.getByText("polling…")).toBeTruthy();
  cleanup();

  await mount("#/", peersFetch(ORIGIN, { [ORIGIN]: { down: new Error("connection refused") } }));
  expect(screen.getByText("polling…")).toBeTruthy();
  cleanup();

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
    expect(document.querySelector("[data-footer-line]")?.textContent).toBe("writer-host · 1 project answering · polled 4s ago");
  } finally {
    globalThis.setInterval = realSetInterval;
  }
});

test("the five page icons differ from one another and from the All projects icon, unfolded and folded", async () => {
  await mount("#/", peersFetch(ORIGIN, { [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: attentionOf([]) } }));
  const drawn = () => {
    const switcher = screen.getByRole("button", { name: /^Project: All projects/ });
    return [...pages(), switcher].map((control) => control.querySelector("svg")!.outerHTML);
  };
  expect(new Set(drawn()).size).toBe(6);
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  expect(new Set(drawn()).size).toBe(6);
});

const DRAG = "[-webkit-app-region:drag]";
const NO_DRAG = "[-webkit-app-region:no-drag]";
const toggle = () => screen.getByRole("button", { name: /^(Collapse|Expand) sidebar$/ });
const headerRow = () => toggle().parentElement!;
const aside = () => screen.getByRole("complementary", { name: "Console" });

test("in a browser the title row holds the panel-left toggle, then the lotus mark alone linking home", async () => {
  await mount("#/");
  const header = headerRow();
  expect(header.classList.contains("h-[52px]")).toBe(true);
  expect(header.classList.contains("pl-3")).toBe(true);
  const [first, second] = Array.from(header.children);
  expect(first).toBe(screen.getByRole("button", { name: "Collapse sidebar" }));
  expect(first!.getAttribute("aria-expanded")).toBe("true");
  expect(first!.classList.contains("size-7")).toBe(true);
  const home = screen.getByRole("link", { name: "holophyte" });
  expect(second).toBe(home);
  expect(home.textContent).toBe("");
  expect(home.getAttribute("title")).toBe("holophyte");
  expect(home.querySelectorAll("svg").length).toBe(1);

  fireEvent.click(first!);
  expect(aside().classList.contains("min-[720px]:w-[56px]")).toBe(true);
  const folded = headerRow();
  expect(folded.classList.contains("h-[52px]")).toBe(true);
  expect(within(folded).getByRole("button", { name: "Expand sidebar" }).getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("link", { name: "holophyte" })).toBeNull();
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

test("in the macOS app the title row clears the traffic lights and drags the window, and its controls do not", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mount("#/");
  const header = headerRow();
  for (const name of ["h-[52px]", "pl-[76px]", DRAG]) expect(header.classList.contains(name)).toBe(true);
  const button = screen.getByRole("button", { name: "Collapse sidebar" });
  const home = screen.getByRole("link", { name: "holophyte" });
  expect(button.compareDocumentPosition(home) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(home.textContent).toBe("");
  for (const control of [button, home]) expect(control.classList.contains(NO_DRAG)).toBe(true);

  fireEvent.click(button);
  expect(aside().classList.contains("min-[720px]:w-[112px]")).toBe(true);
  const folded = headerRow();
  for (const name of ["h-[52px]", "pl-[76px]", DRAG]) expect(folded.classList.contains(name)).toBe(true);
  expect(within(folded).getByRole("button", { name: "Expand sidebar" }).classList.contains(NO_DRAG)).toBe(true);
  expect(screen.queryByRole("link", { name: "holophyte" })).toBeNull();
});

test("in the macOS app a zero-height layer drags the main pane's top 52px and no band pushes the page down", async () => {
  localStorage.setItem(APP_SHELL_KEY, "macos");
  await mount("#/runs");
  const main = screen.getByRole("main");
  expect(main.closest("[data-app-shell]")!.getAttribute("data-app-shell")).toBe("macos");
  const layer = main.firstElementChild as HTMLElement;
  expect(layer.getAttribute("aria-hidden")).toBe("true");
  expect(layer.classList.contains("h-0")).toBe(true);
  expect(layer.children.length).toBe(1);
  const area = layer.firstElementChild as HTMLElement;
  for (const name of [DRAG, "absolute", "h-[52px]"]) expect(area.classList.contains(name)).toBe(true);
  expect(main.querySelectorAll(".h-7").length).toBe(0);
  const regions = Array.from(document.querySelectorAll<HTMLElement>("[class]")).filter((el) => el.classList.contains(DRAG));
  expect(regions).toEqual([headerRow(), area]);
});

test("in a browser nothing names a window drag region or the app shell", async () => {
  await mount("#/runs");
  const named = Array.from(document.querySelectorAll<HTMLElement>("[class]")).filter((el) => el.className.includes("-webkit-app-region"));
  expect(named).toEqual([]);
  expect(document.querySelector("[data-app-shell]")).toBeNull();
  expect(screen.getByRole("main").firstElementChild!.getAttribute("aria-hidden")).not.toBe("true");
});

test("theme.css lets the app's controls inside the main pane cut through its drag area", async () => {
  const css = await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text();
  const rule = declarations(css).find((d) => d.name === "-webkit-app-region" && d.value === "no-drag");
  const selector = rule?.path.at(-1) ?? "";
  expect(selector.startsWith('[data-app-shell="macos"]')).toBe(true);
  for (const name of ["main", "a", "button"]) expect(selector).toMatch(new RegExp(`\\b${name}\\b`));
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

test("each page's heading section opens on the title row's padding", async () => {
  for (const hash of ["#/runs", "#/tickets", "#/findings", "#/settings/general"]) {
    await mount(hash);
    const section = document.getElementById("page-title")!.closest('section[aria-labelledby="page-title"]')!;
    expect(section.classList.contains("pt-3.5")).toBe(true);
    expect(section.classList.contains("py-7")).toBe(false);
    cleanup();
  }
});

test("off Settings the sidebar keeps the project switcher and the Pages nav and holds no Back link", async () => {
  for (const hash of ["#/", "#/runs?project=demo", "#/tickets", "#/findings", "#/scouts", "#/stories"]) {
    await mount(hash);
    const sidebar = within(screen.getByRole("complementary", { name: "Console" }));
    expect(sidebar.getByRole("button", { name: /Switch project$/ })).toBeTruthy();
    expect(sidebar.getByRole("navigation", { name: "Pages" })).toBeTruthy();
    expect(sidebar.queryByRole("link", { name: "Back" })).toBeNull();
    expect(sidebar.queryByRole("navigation", { name: "Settings sections" })).toBeNull();
    cleanup();
  }
});
