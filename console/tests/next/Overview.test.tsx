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

const classes = (element: Element | null | undefined) => element?.getAttribute("class")?.split(/\s+/) ?? [];
const sectionCount = (name: string) => section(name).querySelector("h2 > span")!;

test("the heading is the board's 20 px Archivo 600 title in a header closed by an 18 px pad and a rule hairline", async () => {
  await mount("#/", busy);
  const h1 = screen.getByRole("heading", { level: 1 });
  expect(classes(h1)).toEqual(expect.arrayContaining(["font-sans", "text-[20px]", "font-semibold", "text-ink"]));
  expect(classes(h1)).not.toContain("text-[32px]");
  expect(classes(h1)).not.toContain("font-bold");
  expect(classes(h1.parentElement)).toEqual(expect.arrayContaining(["pb-[18px]", "border-b", "border-rule"]));
  expect(h1.parentElement!.contains(document.querySelector("[data-count-line]"))).toBe(true);
  const number = within(h1).getByText("3");
  expect(classes(number)).toContain("text-need");
});

test("at zero the heading draws Nothing in --live and leaves needs you. in the heading's ink", async () => {
  await mount("#/", fleet({ items: [] }, { items: [] }));
  const h1 = screen.getByRole("heading", { level: 1 });
  expect(h1.textContent).toBe("Nothing needs you.");
  const nothing = within(h1).getByText("Nothing");
  expect(nothing).not.toBe(h1);
  expect(classes(nothing)).toContain("text-live");
  const rest = [...h1.childNodes].filter((node) => node !== nothing);
  expect(rest.map((node) => node.nodeType)).toEqual(rest.map(() => Node.TEXT_NODE));
  expect(classes(h1)).toContain("text-ink");
});

test("the count line sets each number in --ink at weight 600 and its words in --ink-2", async () => {
  await mount("#/", busy);
  const line = document.querySelector("[data-count-line]")!;
  expect(line.textContent).toBe("2 running · 2 open PRs · 1 queued · 1 merged today");
  expect(classes(line)).toEqual(expect.arrayContaining(["font-mono", "text-[13px]", "text-ink-2"]));
  const numbers = [...line.children].filter((child) => /^\d+$/.test(child.textContent ?? ""));
  expect(numbers.map((child) => child.textContent)).toEqual(["2", "2", "1", "1"]);
  for (const number of numbers) expect(classes(number)).toEqual(expect.arrayContaining(["text-ink", "font-semibold"]));
  const separators = [...line.children].filter((child) => child.textContent === " · ");
  expect(separators).toHaveLength(3);
  for (const separator of separators) expect(classes(separator)).toContain("text-ink-3");
});

test("a section heading is the board's chevron, 19 px Archivo 700 name, mono count and mono summary over no rule", async () => {
  await mount("#/", busy);
  const header = section("Needs you").querySelector("header")!;
  const name = within(header).getByRole("button", { name: "Needs you" });
  expect(name.querySelector("svg[data-chevron]")).toBeTruthy();
  expect(classes(name)).toEqual(expect.arrayContaining(["font-sans", "text-[19px]", "font-bold", "text-ink"]));
  expect(sectionCount("Needs you").textContent).toBe("3");
  expect(classes(sectionCount("Needs you"))).toEqual(expect.arrayContaining(["font-mono", "text-need"]));
  const summary = within(header).getByText(/^oldest waiting /);
  expect(classes(summary)).toEqual(expect.arrayContaining(["font-mono", "text-ink-3"]));
  expect(classes(header).filter((name) => name.startsWith("border"))).toEqual([]);
  expect(header.nextElementSibling!.id).toBe(name.getAttribute("aria-controls")!);
});

test("the Needs you count is --need above zero and every other count, or a zero Needs you, is --ink-3", async () => {
  await mount("#/", busy);
  expect(sectionCount("Needs you").textContent).toBe("3");
  expect(classes(sectionCount("Needs you"))).toContain("text-need");
  for (const name of ["Running", "Pull requests", "Merged today"]) {
    expect(classes(sectionCount(name))).toContain("text-ink-3");
    expect(classes(sectionCount(name))).not.toContain("text-need");
  }
  cleanup();

  await mount("#/", fleet({ items: [] }, { items: [] }));
  expect(sectionCount("Needs you").textContent).toBe("0");
  expect(classes(sectionCount("Needs you"))).toContain("text-ink-3");
  expect(classes(sectionCount("Needs you"))).not.toContain("text-need");
  expect(classes(sectionCount("Running"))).toContain("text-ink-3");
  expect(classes(sectionCount("Running"))).not.toContain("text-need");
});

test("an empty section's sentence draws no border of its own, and the sections stand the board's 44 px apart", async () => {
  await mount("#/", fleet({ items: [] }, { items: [] }));
  const regions = screen.getAllByRole("region");
  for (const name of ["Needs you", "Running", "Pull requests", "Merged today"]) {
    const sentence = section(name).querySelector("p")!;
    expect(sentence).toBeTruthy();
    expect(classes(sentence).filter((name) => name.startsWith("border"))).toEqual([]);
  }
  const column = regions[0]!.parentElement!;
  expect(regions.every((region) => region.parentElement === column)).toBe(true);
  expect(classes(column)).toContain("gap-[44px]");
});
