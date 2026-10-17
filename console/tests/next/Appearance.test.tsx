import { afterAll, afterEach, beforeAll, beforeEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import type { Status } from "../../src/lib/types";
import { fakeDeps, NO_ATTENTION, settle, stubFetch } from "../harness";
import { declarations } from "../cssTokens";

const ORIGIN = "http://writer-host:7710";
const status = await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json() as Status;
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
const root = document.documentElement;

async function mount() {
  window.location.hash = "#/settings/appearance";
  const fake = fakeDeps(stubFetch({ status, attention: NO_ATTENTION }));
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
}

function radio(group: string, name: string) {
  return within(screen.getByRole("radiogroup", { name: group })).getByRole("radio", { name: new RegExp(`^${name}\\b`, "i") }) as HTMLInputElement;
}

beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => {
  localStorage.clear();
  root.removeAttribute("data-theme");
  root.removeAttribute("data-font");
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
  localStorage.clear();
  root.removeAttribute("data-theme");
  root.removeAttribute("data-font");
});

test("Appearance and the sidebar share theme choices and System resumes following the computer", async () => {
  let dark = true;
  const changes = new EventTarget();
  const query = {
    get matches() { return dark; },
    media: "(prefers-color-scheme: dark)",
    addEventListener: changes.addEventListener.bind(changes),
    removeEventListener: changes.removeEventListener.bind(changes),
  } as MediaQueryList;
  const media = spyOn(window, "matchMedia").mockReturnValue(query);
  try {
    await mount();
    expect(radio("Theme", "System").checked).toBe(true);
    expect(screen.getByText("now Night, from your computer")).toBeTruthy();
    fireEvent.click(radio("Theme", "Dawn"));
    expect(root.getAttribute("data-theme")).toBe("light");
    expect(localStorage.getItem("holophyte.theme")).toBe("light");
    const toggle = screen.getByRole("button", { name: "Colour theme: dawn. Switch to night" });
    expect(toggle.textContent).toBe("Dawn");
    fireEvent.click(toggle);
    expect(radio("Theme", "Night").checked).toBe(true);
    expect(root.getAttribute("data-theme")).toBe("dark");
    fireEvent.click(radio("Theme", "System"));
    expect(root.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.getItem("holophyte.theme")).toBeNull();
    act(() => {
      dark = false;
      changes.dispatchEvent(new Event("change"));
    });
    expect(radio("Theme", "System").checked).toBe(true);
    expect(screen.getByText("now Dawn, from your computer")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Colour theme: dawn. Switch to night" }).textContent).toBe("Dawn");
  } finally {
    cleanup();
    media.mockRestore();
  }
});

test("reading font choices persist in this browser and Plex Sans removes the override", async () => {
  await mount();
  expect(radio("Reading font", "Plex Sans").checked).toBe(true);
  fireEvent.click(radio("Reading font", "Plex Serif"));
  expect(root.getAttribute("data-font")).toBe("serif");
  expect(localStorage.getItem("holophyte.next.font")).toBe("serif");
  cleanup();
  await mount();
  expect(radio("Reading font", "Plex Serif").checked).toBe(true);
  fireEvent.click(radio("Reading font", "Hyperlegible"));
  expect(root.getAttribute("data-font")).toBe("hyperlegible");
  expect(localStorage.getItem("holophyte.next.font")).toBe("hyperlegible");
  fireEvent.click(radio("Reading font", "Plex Sans"));
  expect(root.hasAttribute("data-font")).toBe(false);
  expect(localStorage.getItem("holophyte.next.font")).toBeNull();
});

test("blocked browser storage still applies reading font and shared theme choices", async () => {
  const blocked = () => { throw new Error("Storage is disabled"); };
  const access = [
    spyOn(Storage.prototype, "getItem").mockImplementation(blocked),
    spyOn(Storage.prototype, "setItem").mockImplementation(blocked),
    spyOn(Storage.prototype, "removeItem").mockImplementation(blocked),
  ];
  try {
    await mount();
    fireEvent.click(radio("Reading font", "Plex Serif"));
    expect(root.getAttribute("data-font")).toBe("serif");
    expect(radio("Reading font", "Plex Serif").checked).toBe(true);
    fireEvent.click(radio("Reading font", "Plex Sans"));
    expect(root.hasAttribute("data-font")).toBe(false);
    expect(radio("Reading font", "Plex Sans").checked).toBe(true);
    fireEvent.click(radio("Theme", "Dawn"));
    expect(root.getAttribute("data-theme")).toBe("light");
    fireEvent.click(screen.getByRole("button", { name: "Colour theme: dawn. Switch to night" }));
    expect(radio("Theme", "Night").checked).toBe(true);
  } finally {
    cleanup();
    for (const mock of access) mock.mockRestore();
  }
});

test("font attributes select the promised reading stacks", async () => {
  const css = declarations(await Bun.file(new URL("../../src/next/theme.css", import.meta.url)).text());
  const font = (selector: string) => css.find((entry) => entry.path.length === 1 && entry.path[0] === selector && entry.name === "--font-read")?.value;
  expect(font(':root[data-font="serif"]')).toBe('"IBM Plex Serif", Georgia, serif');
  expect(font(':root[data-font="hyperlegible"]')).toBe('"Atkinson Hyperlegible Next", "IBM Plex Sans", Arial, sans-serif');
});
