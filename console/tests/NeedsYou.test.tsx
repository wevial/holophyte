import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NeedsYou } from "../src/components/NeedsYou";
import { WRITES_LATER } from "../src/components/ActionButton";
import type { Attention, AttentionItem, Status } from "../src/lib/types";
import { fixture } from "./harness";

const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");

afterEach(cleanup);

const rows = () => screen.getAllByRole("listitem");
const pills = () => rows().map((row) => row.querySelector("[data-kind]")?.textContent);
const chips = () =>
  within(screen.getByRole("group", { name: "Kinds" }))
    .getAllByRole("button")
    .map((chip) => chip.textContent);

test("the fixture renders four things, one chip per kind with counts, rows in daemon order", () => {
  render(<NeedsYou attention={allKinds.attention} status={allKinds.status} project="all" />);
  expect(screen.getByText("4").hasAttribute("data-count")).toBe(true);
  expect(screen.getByText("things need you")).toBeTruthy();
  expect(screen.getByText("oldest 2h · KO-229")).toBeTruthy();
  expect(chips()).toEqual(["All 4", "Questions 1", "Stale runs 1", "Failed 1", "Supervisor 1"]);
  expect(pills()).toEqual(["question", "stale run", "failed", "supervisor"]);
  expect(rows().map((row) => row.getAttribute("data-kind"))).toEqual(["blocked", "stale_run", "failed", "supervisor"]);
  expect(screen.getByText("No heartbeat for 7m 01s while reviewing")).toBeTruthy();
  expect(screen.getByText("Supervisor heartbeat is 20m 00s old (threshold 3m)")).toBeTruthy();
  expect(screen.getAllByText("writer").length).toBe(4);
});

test("the Failed chip keeps only KO-229; All brings the four back", () => {
  render(<NeedsYou attention={allKinds.attention} status={allKinds.status} project="all" />);
  fireEvent.click(screen.getByRole("button", { name: "Failed 1" }));
  expect(rows().length).toBe(1);
  const [row] = rows();
  expect(within(row!).getByText("KO-229")).toBeTruthy();
  expect(within(row!).getByText(/^verify failed/)).toBeTruthy();
  expect(within(row!).getByText("run #88")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Failed 1" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "All 4" }));
  expect(rows().length).toBe(4);
});

test("six questions cap at four with Show all 6, expand, and a chip choice caps again", () => {
  const items: AttentionItem[] = Array.from({ length: 6 }, (_, index) => ({
    kind: "blocked",
    level: "attention",
    ticket: `KO-${300 + index}`,
    question: `Question ${index + 1}?`,
  }));
  render(<NeedsYou attention={{ level: "attention", now: allKinds.status.now, items }} status={allKinds.status} project="all" />);
  expect(rows().length).toBe(4);
  expect(chips()).toEqual(["All 6", "Questions 6"]);
  fireEvent.click(screen.getByRole("button", { name: "Show all 6" }));
  expect(rows().length).toBe(6);
  expect(screen.getByRole("button", { name: "Show fewer" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Questions 6" }));
  expect(rows().length).toBe(4);
  expect(screen.getByRole("button", { name: "Show all 6" })).toBeTruthy();
});

test("every action button is disabled and says writes come later", () => {
  render(<NeedsYou attention={allKinds.attention} status={allKinds.status} project="all" />);
  const actions = rows().flatMap((row) => within(row).getAllByRole("button"));
  expect(actions.map((button) => button.textContent)).toEqual([
    "Answer",
    "Requeue",
    "Kill run",
    "Requeue",
    "Requeue",
    "Mark needs_spec",
    "Restart supervisor",
  ]);
  for (const button of actions) {
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.getAttribute("title")).toBe(WRITES_LATER);
  }
});

test("another project's selection empties the band with the level word; one item reads singular", () => {
  render(<NeedsYou attention={allKinds.attention} status={allKinds.status} project="/srv/dev/other" />);
  expect(screen.getByText("Nothing needs you")).toBeTruthy();
  expect(screen.getByText("attention")).toBeTruthy();
  cleanup();
  const one = { ...allKinds.attention, items: allKinds.attention.items.slice(0, 1) };
  render(<NeedsYou attention={one} status={allKinds.status} project={allKinds.status.target} />);
  expect(screen.getByText("thing needs you")).toBeTruthy();
  expect(rows().length).toBe(1);
});
