import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { THEME_KEY } from "../../src/lib/theme";
import { TICK_MS, type Fetch } from "../../src/lib/poll";
import type { Attention, Status } from "../../src/lib/contract.gen";
import { fakeDeps, peersFetch, settle } from "../harness";
import { needsDaemons } from "./needsFleet";

const ORIGIN = "http://writer-host:7710";
const PEER = "http://writer-host:7711";

const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

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
      <NextConsole base={ORIGIN} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  await act(settle);
  await act(settle);
  return fake;
}

const pages = () => within(screen.getByRole("navigation", { name: "Pages" })).getAllByRole("link");
const badge = () => screen.getByRole("navigation", { name: "Pages" }).querySelector("[data-badge]");

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
  device.prefersColorScheme = "light";
});

test("a scoped hash opens its page, marks its link current and keeps the scope on every page link", async () => {
  await mount("#/runs?project=demo");
  expect((await screen.findByRole("heading", { level: 1 })).textContent).toBe("Runs");
  expect(screen.getByText("demo", { selector: "[data-scope]" })).toBeTruthy();
  const links = pages();
  expect(links.map((link) => link.getAttribute("aria-label"))).toEqual(["Overview", "Runs", "Tickets", "Findings"]);
  expect(links.filter((link) => link.getAttribute("aria-current") === "page").map((link) => link.textContent)).toEqual(["Runs"]);
  for (const link of links) {
    expect(new URLSearchParams(link.getAttribute("href")!.split("?")[1]).get("project")).toBe("demo");
  }
});

test("the Overview badge counts the scope's Needs you entries, as the heading does, and hides at zero", async () => {
  await mount("#/", needsDaemons());
  expect(screen.getByRole("link", { name: "Overview, 3 need you" })).toBeTruthy();
  expect(badge()!.textContent).toBe("3");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("3 need you.");
  cleanup();

  await mount("#/?project=demo", needsDaemons());
  expect(screen.getByRole("link", { name: "Overview, 2 need you" })).toBeTruthy();
  expect(badge()!.textContent).toBe("2");
  cleanup();

  await mount("#/");
  expect(screen.getByRole("link", { name: "Overview" })).toBeTruthy();
  expect(badge()).toBeNull();
});

test("each page links to the classic view behind it, and an unknown route links back to Overview", async () => {
  for (const [hash, name, href] of [
    ["#/runs", "Open Finished in the classic console", "/#view=shipped"],
  ] as const) {
    await mount(hash);
    expect((await screen.findByRole("link", { name })).getAttribute("href")).toBe(href);
    cleanup();
  }
  await mount("#/nowhere");
  expect(await screen.findByText("There is no page here.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Go to Overview" }).getAttribute("href")).toMatch(/#\/$/);
});

test("the fold button folds the sidebar to icons, keeps link names and survives a remount and failing storage", async () => {
  await mount("#/");
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  const expand = screen.getByRole("button", { name: "Expand sidebar" });
  expect(expand.getAttribute("aria-expanded")).toBe("false");
  expect(pages().map((link) => link.textContent)).toEqual(["", "", "", ""]);
  expect(pages().map((link) => link.getAttribute("aria-label"))).toEqual(["Overview", "Runs", "Tickets", "Findings"]);
  for (const name of ["holophyte", "Classic console"]) {
    const link = screen.getByRole("link", { name });
    expect(link.textContent).toBe("");
    expect(link.getAttribute("title")).toBe(name);
  }
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

test("the theme toggle follows a dark system preference, then stores and stamps Dawn", async () => {
  device.prefersColorScheme = "dark";
  await mount("#/");
  const toggle = screen.getByRole("button", { name: "Colour theme: night. Switch to dawn" });
  expect(toggle.textContent).toBe("Night");
  fireEvent.click(toggle);
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem(THEME_KEY)).toBe("light");
  expect(screen.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).textContent).toBe("Dawn");
});

test("with no stored choice the toggle follows a later system preference change", async () => {
  await mount("#/");
  expect(screen.getByRole("button", { name: "Colour theme: dawn. Switch to night" })).toBeTruthy();
  device.prefersColorScheme = "dark";
  try {
    act(() => happyDOM.setViewport({ width: 1000 }));
    fireEvent.click(screen.getByRole("button", { name: "Colour theme: night. Switch to dawn" }));
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
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

test("the four page icons differ from one another and from the All projects icon, unfolded and folded", async () => {
  await mount("#/", peersFetch(ORIGIN, { [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: attentionOf([]) } }));
  const drawn = () => {
    const switcher = screen.getByRole("button", { name: /^Project: All projects/ });
    return [...pages(), switcher].map((control) => control.querySelector("svg")!.outerHTML);
  };
  expect(new Set(drawn()).size).toBe(5);
  fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
  expect(new Set(drawn()).size).toBe(5);
});
