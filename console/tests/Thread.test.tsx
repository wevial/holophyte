import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { WRITES_LATER } from "../src/components/ActionButton";
import { NeedsYou } from "../src/components/NeedsYou";
import { Now } from "../src/components/Now";
import { ANSWER_PLACEHOLDER } from "../src/components/QuestionThread";
import type { Ledgers } from "../src/hooks/useLedger";
import { EVIDENCE_MS, localMidnight, type LedgerRow } from "../src/lib/ledger";
import type { Fetch } from "../src/lib/poll";
import type { Attention, AttentionItem, Status } from "../src/lib/types";
import { fixture, hostOf, settle } from "./harness";

const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");
const BASE = "http://writer:7710";
const NOW = allKinds.status.now;
const MIDNIGHT = localMidnight(NOW);
const min = (n: number) => n * 60_000;
const ASKED = NOW - min(30);
/** Where the console's ledger window starts with no question older than it. */
const WINDOW = MIDNIGHT - EVIDENCE_MS;

afterEach(cleanup);

const rows = () => screen.getAllByRole("listitem");
const question = (ticket: string) => rows().find((row) => within(row).queryByText(ticket) != null)!;

/** KO-240 and KO-241 blocked on run 95 and 96, each asked half an hour ago. */
function blocked(): Attention {
  const items: AttentionItem[] = [
    { kind: "blocked", level: "attention", ticket: "KO-240", run: 95, question: "Which path is the contract?", asked_ms: ASKED },
    { kind: "blocked", level: "attention", ticket: "KO-241", run: 96, question: "Keep the flag name?", asked_ms: ASKED },
  ];
  return { level: "attention", now: NOW, items };
}

const THREAD: LedgerRow[] = [
  { at: ASKED + min(12), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: the ticket body's" },
  { at: ASKED + min(1), run: 95, ticket: "KO-240", kind: "note", source: "loop", text: "Parked blocked_on_operator." },
];

test("activating a question row opens its thread oldest-first with the disabled footer; a second question closes the first", () => {
  const ledgers: Ledgers = { "writer:7710": { rows: THREAD, absent: false } };
  render(<NeedsYou hosts={[hostOf(allKinds.status, blocked(), BASE)]} project="all" now={NOW} ledgers={ledgers} />);
  const first = question("KO-240");
  const toggle = within(first).getByRole("button", { expanded: false });
  expect(within(first).getByText("thread ▾")).toBeTruthy();
  expect(document.querySelector("[data-thread]")).toBeNull();

  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(within(first).getByText("hide thread ▴")).toBeTruthy();
  const card = first.querySelector("[data-thread]")!;
  const lines = Array.from(card.querySelectorAll("[data-thread-row]")).map((line) => [
    line.querySelector("[data-who]")!.textContent,
    line.children[1]!.textContent,
  ]);
  expect(lines).toEqual([
    ["run #95", "Which path is the contract?"],
    ["ledger", "Parked blocked_on_operator."],
    ["operator", "human resume: the ticket body's"],
  ]);
  const field = within(card as HTMLElement).getByPlaceholderText(ANSWER_PLACEHOLDER) as HTMLTextAreaElement;
  expect(field.disabled).toBe(true);
  const answer = within(card as HTMLElement).getByRole("button", { name: "Answer & resume" }) as HTMLButtonElement;
  expect(answer.disabled).toBe(true);
  expect(answer.getAttribute("title")).toBe(WRITES_LATER);

  const second = question("KO-241");
  fireEvent.click(within(second).getByRole("button", { expanded: false }));
  expect(document.querySelectorAll("[data-thread]").length).toBe(1);
  expect(second.querySelector("[data-thread]")).toBeTruthy();
  expect(first.querySelector("[data-thread]")).toBeNull();
  expect(within(first).getByText("thread ▾")).toBeTruthy();
});

/** A daemon serving `/status`, `/attention` and a `/ledger` window: like
 *  the daemon, it answers only the entries at or after `since`. */
function ledgerFetch(attention: Attention, entries: LedgerRow[] | 404, asked: string[] = []): Fetch {
  return async (url) => {
    asked.push(url);
    if (url.endsWith("/status")) return Response.json(allKinds.status);
    if (url.endsWith("/attention")) return Response.json(attention);
    if (url.includes("/ledger?")) {
      if (entries === 404) return new Response("not found", { status: 404 });
      const since = Number(new URL(url).searchParams.get("since"));
      return Response.json({ entries: entries.filter((row) => row.at >= since), since, limit: 1000 });
    }
    return new Response("not found", { status: 404 });
  };
}

const RESOLVED: LedgerRow[] = [
  { at: MIDNIGHT + min(141), run: 88, ticket: "KO-229", kind: "intervention", source: "operator", text: "human requeue: fixed the fixture" },
  { at: MIDNIGHT + min(131), run: 91, ticket: "KO-232", kind: "intervention", source: "loop", text: "supervisor kill: no heartbeat" },
  { at: MIDNIGHT + min(126), run: 91, ticket: "KO-232", kind: "failure", source: "loop", text: "Strike 1: stale" },
  { at: MIDNIGHT + min(74), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: the ticket body's" },
  { at: MIDNIGHT + min(60), run: 95, ticket: "KO-240", kind: "note", source: "loop", text: "Parked." },
];

test("the resolved fold counts today's interventions, opens to their rows with waits, and asks /ledger from a day before midnight", async () => {
  const asked: string[] = [];
  // KO-229 failed 41 minutes before its requeue; the fixture's item carries that `ended_ms`.
  const failedAt = MIDNIGHT + min(100);
  const attention: Attention = {
    ...allKinds.attention,
    items: allKinds.attention.items.map((item) => (item.kind === "failed" ? { ...item, ended_ms: failedAt } : item)),
  };
  render(<Now hosts={[hostOf(allKinds.status, attention, BASE)]} project="all" now={NOW} deps={{ fetch: ledgerFetch(attention, RESOLVED, asked) }} />);
  await act(settle);
  expect(asked.filter((url) => url.includes("/ledger?"))).toEqual([`${BASE}/ledger?since=${WINDOW}&limit=1000`]);
  const fold = screen.getByRole("region", { name: "Resolved today" });
  const strip = within(fold).getByRole("button", { expanded: false });
  expect(strip.textContent).toContain("Resolved today · 3");
  expect(strip.textContent).toContain("median wait 14m · longest 41m");
  expect(fold.querySelectorAll("[data-resolved-row]").length).toBe(0);

  fireEvent.click(strip);
  expect(strip.getAttribute("aria-expanded")).toBe("true");
  const shown = Array.from(fold.querySelectorAll("[data-resolved-row]")).map((row) =>
    Array.from(row.querySelectorAll("span, div")).map((cell) => cell.textContent),
  );
  expect(shown.map((cells) => cells.slice(1, 6))).toEqual([
    ["failed", "KO-229", "human requeue: fixed the fixture", "waited 41m", "operator"],
    ["stale run", "KO-232", "supervisor kill: no heartbeat", "waited 5m", "supervisor"],
    ["question", "KO-240", "human resume: the ticket body's", "waited 14m", "operator"],
  ]);
  expect(fold.querySelectorAll("[data-resolved-row] [data-kind]").length).toBe(3);
});

test("with nothing resolved today the strip says 0 and opens to say so; a question older than the window widens it", async () => {
  const asked: string[] = [];
  const yesterday = WINDOW - min(90);
  const attention: Attention = {
    level: "attention",
    now: NOW,
    items: [{ kind: "blocked", level: "attention", ticket: "KO-240", run: 95, question: "Which path?", asked_ms: yesterday }],
  };
  render(<Now hosts={[hostOf(allKinds.status, attention, BASE)]} project="all" now={NOW} deps={{ fetch: ledgerFetch(attention, [], asked) }} />);
  await act(settle);
  expect(asked.filter((url) => url.includes("/ledger?"))).toEqual([`${BASE}/ledger?since=${yesterday}&limit=1000`]);
  const fold = screen.getByRole("region", { name: "Resolved today" });
  const strip = within(fold).getByRole("button");
  expect(strip.textContent).toBe("▸Resolved today · 0");
  fireEvent.click(strip);
  expect(within(fold).getByText("Nothing resolved yet today")).toBeTruthy();
});

test("a question parked last night and answered before the console loaded still shows its wait", async () => {
  // Parked at 23:30, answered at 00:10, console loaded later: only the ledger holds the wait's start.
  const overnight: LedgerRow[] = [
    { at: MIDNIGHT + min(10), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: go" },
    { at: MIDNIGHT - min(30), run: 95, ticket: "KO-240", kind: "note", source: "loop", text: "Parked." },
  ];
  const quiet: Attention = { level: "none", now: NOW, items: [] };
  render(<Now hosts={[hostOf(allKinds.status, quiet, BASE)]} project="all" now={NOW} deps={{ fetch: ledgerFetch(quiet, overnight) }} />);
  await act(settle);
  const fold = screen.getByRole("region", { name: "Resolved today" });
  const strip = within(fold).getByRole("button");
  expect(strip.textContent).toContain("Resolved today · 1");
  expect(strip.textContent).toContain("median wait 40m · longest 40m");
  fireEvent.click(strip);
  expect(within(fold).getByText("waited 40m")).toBeTruthy();
});

test("two daemons sharing a run id pair each resolution with their own question", async () => {
  // Both daemons parked run 95; A asked 55 minutes before its answer, B 15 minutes before its own.
  const other = "http://reader:7710";
  const asks = (askedAt: number, ticket: string): Attention => ({
    level: "attention",
    now: NOW,
    items: [{ kind: "blocked", level: "attention", ticket, run: 95, question: "Which?", asked_ms: askedAt }],
  });
  const a = asks(MIDNIGHT + min(5), "KO-240");
  const b = asks(MIDNIGHT + min(45), "KO-300");
  const ledgerA: LedgerRow[] = [
    { at: MIDNIGHT + min(60), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: a" },
  ];
  const ledgerB: LedgerRow[] = [
    { at: MIDNIGHT + min(60), run: 95, ticket: "KO-300", kind: "intervention", source: "operator", text: "human resume: b" },
  ];
  const fetchA = ledgerFetch(a, ledgerA);
  const fetchB = ledgerFetch(b, ledgerB);
  const fetch: Fetch = (url, init) => (url.startsWith(other) ? fetchB(url, init) : fetchA(url, init));
  render(
    <Now hosts={[hostOf(allKinds.status, a, BASE), hostOf(allKinds.status, b, other)]} project="all" now={NOW} deps={{ fetch }} />,
  );
  await act(settle);
  const fold = screen.getByRole("region", { name: "Resolved today" });
  const strip = within(fold).getByRole("button");
  expect(strip.textContent).toContain("Resolved today · 2");
  expect(strip.textContent).toContain("median wait 35m · longest 55m");
  fireEvent.click(strip);
  const waits = Array.from(fold.querySelectorAll("[data-resolved-row]")).map((row) => [
    within(row as HTMLElement).getByText(/KO-/).textContent,
    within(row as HTMLElement).getByText(/waited/).textContent,
  ]);
  expect(waits).toEqual([
    ["KO-240", "waited 55m"],
    ["KO-300", "waited 15m"],
  ]);
});

test("a daemon without /ledger shows no thread hint and no fold, and the band renders as before", async () => {
  render(<Now hosts={[hostOf(allKinds.status, blocked(), BASE)]} project="all" now={NOW} deps={{ fetch: ledgerFetch(blocked(), 404) }} />);
  await act(settle);
  expect(screen.queryByRole("region", { name: "Resolved today" })).toBeNull();
  expect(screen.queryByText("thread ▾")).toBeNull();
  const first = question("KO-240");
  expect(first.querySelector("[aria-expanded]")).toBeNull();
  expect(within(first).getAllByRole("button").map((button) => button.textContent)).toEqual(["Answer", "Requeue"]);
  expect(screen.getByText("things need you")).toBeTruthy();
  expect(within(screen.getByRole("region", { name: "Needs you" })).getAllByRole("listitem").length).toBe(2);
});
