import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NeedsYou } from "../src/components/NeedsYou";
import { ACTIONS_OFF, NOT_WIRED, ROUTES } from "../src/lib/actions";
import type { Attention, AttentionItem, Status } from "../src/lib/types";
import { fixture, hostOf } from "./harness";

const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");

afterEach(cleanup);

const rows = () => screen.getAllByRole("listitem");
const pills = () => rows().map((row) => row.querySelector("[data-kind]")?.textContent);
const chips = () =>
  within(screen.getByRole("group", { name: "Kinds" }))
    .getAllByRole("button")
    .map((chip) => chip.textContent);

test("the fixture renders four things, one chip per kind with counts, rows in daemon order", () => {
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
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
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
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
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  expect(rows().length).toBe(4);
  expect(chips()).toEqual(["All 6", "Questions 6"]);
  fireEvent.click(screen.getByRole("button", { name: "Show all 6" }));
  expect(rows().length).toBe(6);
  expect(screen.getByRole("button", { name: "Show fewer" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Questions 6" }));
  expect(rows().length).toBe(4);
  expect(screen.getByRole("button", { name: "Show all 6" })).toBeTruthy();
});

test("a question row with pr_url shows a PR #N anchor on its line; the fixture's rows without one show none", () => {
  const url = "https://github.com/o/r/pull/2170";
  const items: AttentionItem[] = [
    { kind: "blocked", level: "attention", ticket: "KO-335", run: 50, question: "Merge the PR?", pr_url: url },
    { kind: "blocked", level: "attention", ticket: "KO-336", run: 51, question: "Which branch?", pr_url: null },
  ];
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  const [withPr, without] = rows();
  const pr = within(withPr!).getByText("PR #2170") as HTMLAnchorElement;
  expect(pr.tagName).toBe("A");
  expect(pr.getAttribute("href")).toBe(url);
  expect(pr.getAttribute("target")).toBe("_blank");
  expect(pr.getAttribute("rel")).toBe("noopener noreferrer");
  expect(within(withPr!).getByText(/Merge the PR\?/)).toBeTruthy();
  expect(without!.querySelector("[data-pr]")).toBeNull();
  cleanup();

  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
  expect(document.querySelector("[data-pr]")).toBeNull();
});

test("every action button of a daemon without actions is disabled: wired ones name the opt-in, the rest not wired yet", () => {
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="all" now={allKinds.status.now} />);
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
    expect(button.getAttribute("title")).toBe(button.textContent! in ROUTES ? ACTIONS_OFF : NOT_WIRED);
  }
});

test("another project's selection empties the band with the level word; one item reads singular", () => {
  render(<NeedsYou hosts={[hostOf(allKinds.status, allKinds.attention)]} project="/srv/dev/other" now={allKinds.status.now} />);
  expect(screen.getByText("Nothing needs you")).toBeTruthy();
  expect(screen.getByText("attention")).toBeTruthy();
  cleanup();
  const one = { ...allKinds.attention, items: allKinds.attention.items.slice(0, 1) };
  render(<NeedsYou hosts={[hostOf(allKinds.status, one)]} project={allKinds.status.target} now={allKinds.status.now} />);
  expect(screen.getByText("thing needs you")).toBeTruthy();
  expect(rows().length).toBe(1);
});

test("a pr_open item adds a PRs chip that filters to it, and the total counts it with the rest", () => {
  const url = "https://github.com/o/r/pull/2170";
  const [question, ...rest] = allKinds.attention.items;
  const items: AttentionItem[] = [
    question!,
    { kind: "pr_open", level: "attention", ticket: "REL-120", run: 60, pr_url: url, reason: "review requested", asked_ms: allKinds.status.now - 600000 },
    ...rest,
  ];
  render(<NeedsYou hosts={[hostOf(allKinds.status, { level: "attention", now: allKinds.status.now, items })]} project="all" now={allKinds.status.now} />);
  expect(screen.getByText("5").hasAttribute("data-count")).toBe(true);
  expect(chips()).toEqual(["All 5", "Questions 1", "PRs 1", "Stale runs 1", "Failed 1", "Supervisor 1"]);
  expect(pills()).toEqual(["question", "PR", "stale run", "failed"]);
  expect(screen.getByRole("button", { name: "Show all 5" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "PRs 1" }));
  expect(rows().length).toBe(1);
  const [row] = rows();
  expect(row!.getAttribute("data-kind")).toBe("pr_open");
  expect(within(row!).getByText("REL-120")).toBeTruthy();
  expect(within(row!).getByText(/^review requested/)).toBeTruthy();
  expect((within(row!).getByText("PR #2170") as HTMLAnchorElement).getAttribute("href")).toBe(url);
  expect(within(row!).getAllByRole("button").map((b) => b.textContent)).toEqual(["Open PR"]);
});
