import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";
import { blocked, ciPark, DEMO, failed, fleet, merged, MINUTE, NOW, prPark, queuedOf, run, SERVE } from "./needsFleet";

const busy = fleet(
  {
    items: [blocked, ...failed, ciPark],
    runs: [run(3, "DEMO-3"), run(7, "DEMO-7")],
    queued: queuedOf("DEMO-2"),
    shipped: [merged(10, "DEMO-10", NOW - 30 * MINUTE)],
  },
  { items: [prPark] },
);

async function mount(hash: string, fetchImpl: Fetch) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const heading = () => screen.queryByRole("heading", { level: 1 })?.textContent ?? null;
const countLine = () => document.querySelector("[data-count-line]")?.textContent ?? null;
const section = (name: string) => screen.getByRole("region", { name });
const sectionHeader = (name: string) => section(name).querySelector("header")!.textContent;

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("unscoped, the heading counts every host's Needs you entries and the count line every host's work", async () => {
  await mount("#/", busy);
  expect(heading()).toBe("3 need you.");
  expect(countLine()).toBe("2 running · 2 open PRs · 1 queued · 1 merged today");
});

test("a picked project scopes the heading, the count line and the Pull requests count", async () => {
  await mount("#/?project=demo", busy);
  expect(heading()).toBe("2 need you.");
  expect(countLine()).toBe("2 running · 1 open PR · 1 queued · 1 merged today");
  expect(sectionHeader("Pull requests")).toBe("Pull requests1none wait on you");
  cleanup();

  await mount("#/?project=demo", fleet({ items: [blocked] }, { items: [prPark] }));
  expect(heading()).toBe("1 needs you.");
  cleanup();

  await mount("#/?project=demo", fleet({ items: [] }, { items: [prPark] }));
  expect(heading()).toBe("Nothing needs you.");
});

test("the four sections stand in order with their counts and summaries", async () => {
  await mount("#/", busy);
  const labels = screen.getAllByRole("region").map((region) => region.getAttribute("aria-label"));
  expect(labels).toEqual(["Needs you", "Running", "Pull requests", "Merged today"]);
  expect(sectionHeader("Needs you")).toBe("Needs you3oldest waiting 8h 02m");
  expect(sectionHeader("Running")).toBe("Running22 of 2 workers busy · 1 queued");
  expect(sectionHeader("Pull requests")).toBe("Pull requests21 waits on you");
  expect(sectionHeader("Merged today")).toBe("Merged today1last at 11:30");
});

test("a folded section stays folded across a remount, and folds with storage throwing", async () => {
  await mount("#/", busy);
  const label = () => within(section("Running")).getByRole("button", { name: "Running" });
  const body = () => document.getElementById(label().getAttribute("aria-controls")!)!;
  expect(label().getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(label());
  expect(label().getAttribute("aria-expanded")).toBe("false");
  expect(body().hidden).toBe(true);
  expect(localStorage.getItem("holophyte.next.fold.run")).not.toBeNull();
  cleanup();

  await mount("#/", busy);
  expect(label().getAttribute("aria-expanded")).toBe("false");
  expect(body().hidden).toBe(true);
  cleanup();

  const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    get: () => {
      throw new Error("storage disabled");
    },
  });
  try {
    await mount("#/", busy);
    expect(label().getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(label());
    expect(label().getAttribute("aria-expanded")).toBe("false");
    expect(body().hidden).toBe(true);
  } finally {
    Object.defineProperty(globalThis, "localStorage", real);
  }
});

test("empty sections say what the factory is doing, naming the queue and the picked project", async () => {
  const quiet = fleet({ items: [], queued: queuedOf("DEMO-2", "DEMO-12") }, { items: [] });
  await mount("#/", quiet);
  expect(within(section("Running")).getByText("Nothing running. DEMO-2 and DEMO-12 are queued.")).toBeTruthy();
  expect(within(section("Needs you")).getByText("The factory has it from here.")).toBeTruthy();
  expect(within(section("Pull requests")).getByText("No pull requests open.")).toBeTruthy();
  expect(within(section("Merged today")).getByText("Nothing merged yet today.")).toBeTruthy();
  cleanup();

  await mount("#/?project=demo", quiet);
  expect(within(section("Needs you")).getByText("Nothing in demo is waiting on you.")).toBeTruthy();
});

test("the queue sentence names one key, or three and how many more", async () => {
  await mount("#/?project=demo", fleet({ items: [], queued: queuedOf("DEMO-2") }, { items: [] }));
  expect(within(section("Running")).getByText("Nothing running. DEMO-2 is queued.")).toBeTruthy();
  cleanup();

  const five = queuedOf("DEMO-2", "DEMO-3", "DEMO-4", "DEMO-5", "DEMO-6");
  await mount("#/?project=demo", fleet({ items: [], queued: five }, { items: [] }));
  expect(within(section("Running")).getByText("Nothing running. DEMO-2, DEMO-3, DEMO-4 and 2 more are queued.")).toBeTruthy();
});

test("before any scoped host answers there is no heading and no count line", async () => {
  await mount("#/", () => new Promise<Response>(() => {}));
  expect(heading()).toBeNull();
  expect(countLine()).toBeNull();
  expect(screen.queryByText("Nothing needs you.")).toBeNull();
  expect(section("Needs you")).toBeTruthy();
});

test("a host daemon that answered with no projects draws the heading and a zero count line", async () => {
  const hostStatus = await Bun.file(new URL("host-status.json", SERVE)).json();
  const empty = peersFetch(DEMO, {
    [DEMO]: { status: { ...hostStatus, projects: [] }, attention: { level: "none", now: NOW, items: [] } },
  });
  await mount("#/", empty);
  expect(heading()).toBe("Nothing needs you.");
  expect(countLine()).toBe("0 running · 0 open PRs · 0 queued · 0 merged today");
});
