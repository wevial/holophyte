import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { App } from "../src/App";
import { THEME_KEY } from "../src/lib/theme";
import type { Attention, Status } from "../src/lib/types";
import { NO_ATTENTION, fakeDeps, fixture, settle, stubFetch } from "./harness";

const BASE = "http://writer:7710";

const working = await fixture<Status>("working.json");
const idle = await fixture<Status>("idle.json");
const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");

async function mount(bodies: { status: Status; attention: Attention }) {
  const { deps } = fakeDeps(stubFetch(bodies));
  render(<App base={BASE} pollDeps={deps} />);
  await act(settle);
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterEach(cleanup);

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

test("no stored theme leaves the document unstamped; choosing Light stamps and persists it", async () => {
  await mount({ status: idle, attention: NO_ATTENTION });
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(screen.getByRole("button", { name: "System" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "Light" }));
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  expect(localStorage.getItem(THEME_KEY)).toBe("light");
  expect(screen.getByRole("button", { name: "Light" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "System" }));
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  expect(localStorage.getItem(THEME_KEY)).toBeNull();
});
