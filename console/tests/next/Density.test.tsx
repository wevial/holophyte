import { afterAll, afterEach, beforeAll, beforeEach, expect, spyOn, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Board, History } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { writeView } from "../../src/next/components/ViewToggle";
import { fakeDeps, settle } from "../harness";
import { declarations } from "../cssTokens";
import { DEMO, fleet, NOW, prPark, queuedOf, run, SERVE, WRITER } from "./needsFleet";

const board = await Bun.file(new URL("board.json", SERVE)).json() as Board;
const history = await Bun.file(new URL("history.json", SERVE)).json() as History;
const hosts = fleet({ items: [], runs: [run(3, "DEMO-3"), run(7, "DEMO-7")], queued: queuedOf("DEMO-2") }, { items: [prPark] });
const fetch: Fetch = async (url) => {
  if (url === `${DEMO}/board`) return Response.json(board);
  if (url.startsWith(`${DEMO}/history?`)) {
    return Response.json({ ...history, now: NOW, rows: history.rows.map((row, index) => ({ ...row, id: index + 1, started_ms: NOW - 60_000 })) });
  }
  if (url.startsWith(`${WRITER}/history?`)) return Response.json({ ...history, now: NOW, rows: [] });
  return hosts(url);
};
const root = document.documentElement;
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
const choice = (name: string) => within(screen.getByRole("group", { name: "Density" })).getByRole("button", { name });

async function mount(hash = "#/settings/appearance") {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  for (let round = 0; round < 4; round += 1) await act(settle);
}

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  root.removeAttribute("data-density");
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
  localStorage.clear();
  root.removeAttribute("data-density");
});

test("density defaults to Comfortable, persists Compact across a remount, and removes the Comfortable override", async () => {
  await mount();
  expect(choice("Comfortable").getAttribute("aria-pressed")).toBe("true");
  expect(choice("Compact").getAttribute("aria-pressed")).toBe("false");
  expect(root.hasAttribute("data-density")).toBe(false);
  fireEvent.click(choice("Compact"));
  expect(choice("Compact").getAttribute("aria-pressed")).toBe("true");
  expect(root.getAttribute("data-density")).toBe("compact");
  expect(localStorage.getItem("holophyte.next.density")).toBe("compact");
  cleanup();
  await mount();
  expect(choice("Compact").getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(choice("Comfortable"));
  expect(choice("Comfortable").getAttribute("aria-pressed")).toBe("true");
  expect(choice("Compact").getAttribute("aria-pressed")).toBe("false");
  expect(root.hasAttribute("data-density")).toBe(false);
  expect(localStorage.getItem("holophyte.next.density")).toBeNull();
  const preview = screen.getByRole("region", { name: "Preview" });
  expect(preview.querySelectorAll("[data-row]")).toHaveLength(2);
});

test("density still switches the document and pressed choice when every storage operation throws", async () => {
  const blocked = () => { throw new Error("Storage is disabled"); };
  const access = [
    spyOn(Storage.prototype, "getItem").mockImplementation(blocked),
    spyOn(Storage.prototype, "setItem").mockImplementation(blocked),
    spyOn(Storage.prototype, "removeItem").mockImplementation(blocked),
  ];
  try {
    await mount();
    fireEvent.click(choice("Compact"));
    expect(root.getAttribute("data-density")).toBe("compact");
    expect(choice("Compact").getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(choice("Comfortable"));
    expect(root.hasAttribute("data-density")).toBe(false);
    expect(choice("Comfortable").getAttribute("aria-pressed")).toBe("true");
  } finally {
    cleanup();
    for (const mock of access) mock.mockRestore();
  }
});

test("every Overview running, queued and pull request row and Runs and Tickets table row opts into density", async () => {
  await mount("#/");
  const running = screen.getByRole("region", { name: "Running" });
  const liveRows = within(running).getAllByRole("link", { name: /^DEMO-(3|7) / });
  expect(liveRows).toHaveLength(2);
  for (const row of liveRows) expect(row.hasAttribute("data-row")).toBe(true);
  fireEvent.click(within(running).getByRole("button", { name: "1 queued" }));
  const queued = running.querySelectorAll("[data-queued]");
  expect(queued).toHaveLength(1);
  for (const row of queued) expect(row.hasAttribute("data-row")).toBe(true);
  const prs = screen.getByRole("region", { name: "Pull requests" }).querySelectorAll("[data-pr-row] > div:first-child");
  expect(prs).toHaveLength(1);
  for (const row of prs) expect(row.hasAttribute("data-row")).toBe(true);
  cleanup();

  await mount("#/runs");
  const runs = within(screen.getByRole("list", { name: "Runs, newest first" })).getAllByRole("listitem");
  expect(runs).toHaveLength(3);
  for (const row of runs) {
    expect(row.hasAttribute("data-row")).toBe(true);
  }
  cleanup();

  writeView("table");
  await mount("#/tickets");
  const tickets = document.querySelectorAll("[data-ticket-table] [data-lane-rows] > li > a");
  expect(tickets.length).toBeGreaterThan(0);
  for (const row of tickets) expect(row.hasAttribute("data-row")).toBe(true);
});

test("the shared compact rule tightens marked rows to a 44 px minimum and 6 px block padding", async () => {
  const css = declarations(await Bun.file(new URL("../../src/next/theme.css", import.meta.url)).text());
  const compact = css.filter((entry) => entry.path.length === 1 && entry.path[0] === ':root[data-density="compact"] [data-row]');
  expect(compact.find((entry) => entry.name === "min-height")?.value).toBe("44px");
  expect(compact.find((entry) => entry.name === "padding-block")?.value).toBe("6px");
});
