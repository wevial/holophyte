import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NextConsole } from "../../src/next/router";
import { THEME_KEY } from "../../src/lib/theme";
import type { Fetch } from "../../src/lib/poll";
import type { Attention, AttentionItem, Status } from "../../src/lib/types";
import { NO_ATTENTION, fakeDeps, fixture, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const PEER = "http://writer-host:7711";

const working = await fixture<Status>("working.json");
const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");

const happyDOM = (window as unknown as {
  happyDOM: { settings: { device: { prefersColorScheme: string } }; setViewport: (viewport: { width: number }) => void };
}).happyDOM;
const device = happyDOM.settings.device;

const statusOf = (project: string): Status => ({ ...working, host: "writer-host", project });
const attentionOf = (items: AttentionItem[]): Attention => ({ level: items.length ? "attention" : "none", now: 0, items });

function daemons(demoItems: AttentionItem[], writerItems: AttentionItem[]) {
  return peersFetch(ORIGIN, {
    [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: attentionOf(demoItems) },
    [PEER]: { status: statusOf("/srv/dev/writer"), attention: attentionOf(writerItems) },
  });
}

async function mount(hash: string, fetchImpl: Fetch = daemons([], [])) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  render(<NextConsole base={ORIGIN} pollDeps={fake.deps} />);
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

test("the Overview badge counts the scope's attention items and hides at zero", async () => {
  const [one, two, three] = allKinds.attention.items as [AttentionItem, AttentionItem, AttentionItem];
  await mount("#/", daemons([one, two], [three]));
  expect(screen.getByRole("link", { name: "Overview, 3 need you" })).toBeTruthy();
  expect(badge()!.textContent).toBe("3");
  cleanup();

  await mount("#/?project=demo", daemons([one, two], [three]));
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
    ["#/tickets", "Open the Board in the classic console", "/#view=board"],
    ["#/", "Open Now in the classic console", "/"],
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

  const { clock, fireTick } = await mount("#/", peersFetch(ORIGIN, {
    [ORIGIN]: { status: statusOf("/srv/dev/demo"), attention: NO_ATTENTION },
  }));
  clock.now = 4000;
  act(fireTick);
  expect(screen.getByText("writer-host · polled 4s ago")).toBeTruthy();
});
