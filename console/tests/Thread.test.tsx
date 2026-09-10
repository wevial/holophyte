import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { WRITES_LATER } from "../src/components/ActionButton";
import { NeedsYou } from "../src/components/NeedsYou";
import { Now } from "../src/components/Now";
import { ANSWER_PLACEHOLDER } from "../src/components/QuestionThread";
import type { Ledgers } from "../src/hooks/useLedger";
import { localMidnight, type LedgerRow } from "../src/lib/ledger";
import type { Fetch } from "../src/lib/poll";
import type { Attention, AttentionItem, Status } from "../src/lib/types";
import { fixture, hostOf, settle } from "./harness";

const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");
const BASE = "http://writer:7710";
const NOW = allKinds.status.now;
const MIDNIGHT = localMidnight(NOW);
const min = (n: number) => n * 60_000;
const ASKED = NOW - min(30);

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
  const ledgers: Ledgers = { "writer:7710": { rows: [], threads: { "KO-240": THREAD }, absent: false } };
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

/** A daemon serving `/status`, `/attention` and `/ledger`: like the
 *  daemon, it answers the entries at or after `since`, narrowed to one
 *  `kind` or `ticket` when asked, newest first and cut at `limit`. */
function ledgerFetch(attention: Attention, entries: LedgerRow[] | 404, asked: string[] = []): Fetch {
  return async (url) => {
    asked.push(url);
    if (url.endsWith("/status")) return Response.json(allKinds.status);
    if (url.endsWith("/attention")) return Response.json(attention);
    if (url.includes("/ledger?")) {
      if (entries === 404) return new Response("not found", { status: 404 });
      const params = new URL(url).searchParams;
      const since = Number(params.get("since"));
      const kind = params.get("kind");
      const ticket = params.get("ticket");
      const limit = Number(params.get("limit") ?? 200);
      const page = entries
        .filter((row) => row.at >= since && (kind == null || row.kind === kind) && (ticket == null || row.ticket === ticket))
        .sort((a, b) => b.at - a.at)
        .slice(0, limit);
      return Response.json({ entries: page, since, limit });
    }
    return new Response("not found", { status: 404 });
  };
}

const ledgerAsks = (asked: string[]) => asked.filter((url) => url.includes("/ledger?"));
const windowUrl = `${BASE}/ledger?since=${MIDNIGHT}&kind=intervention&limit=1000`;
const threadUrl = (ticket: string, since: number) => `${BASE}/ledger?since=${since}&ticket=${ticket}&limit=1000`;

/** Today's three interventions as KO-308's daemon serves them: what each
 *  cleared and how long that waited, one with neither. */
const RESOLVED: LedgerRow[] = [
  { at: MIDNIGHT + min(141), run: 88, ticket: "KO-229", kind: "intervention", source: "operator", text: "human requeue: fixed the fixture", cleared: "failed", waited_ms: 2_460_000 },
  { at: MIDNIGHT + min(131), run: 91, ticket: "KO-232", kind: "intervention", source: "loop", text: "supervisor kill: no heartbeat", cleared: null, waited_ms: null },
  { at: MIDNIGHT + min(126), run: 91, ticket: "KO-232", kind: "failure", source: "loop", text: "Strike 1: stale" },
  { at: MIDNIGHT + min(74), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: the ticket body's", cleared: "question", waited_ms: 840_000 },
  { at: MIDNIGHT + min(60), run: 95, ticket: "KO-240", kind: "note", source: "loop", text: "Parked." },
];

test("the resolved fold counts today's interventions, opens to their rows with each entry's own wait, and asks /ledger from midnight", async () => {
  const asked: string[] = [];
  render(<Now hosts={[hostOf(allKinds.status, allKinds.attention, BASE)]} project="all" now={NOW} deps={{ fetch: ledgerFetch(allKinds.attention, RESOLVED, asked) }} />);
  await act(settle);
  expect(ledgerAsks(asked)).toEqual([windowUrl, threadUrl("KO-240", MIDNIGHT)]);
  const fold = screen.getByRole("region", { name: "Resolved today" });
  const strip = within(fold).getByRole("button", { expanded: false });
  expect(strip.textContent).toContain("Resolved today · 3");
  expect(strip.textContent).toContain("median wait 27m · longest 41m");
  expect(fold.querySelectorAll("[data-resolved-row]").length).toBe(0);

  fireEvent.click(strip);
  expect(strip.getAttribute("aria-expanded")).toBe("true");
  const shown = Array.from(fold.querySelectorAll("[data-resolved-row]")).map((row) =>
    Array.from(row.querySelectorAll("span, div")).map((cell) => cell.textContent),
  );
  expect(shown.map((cells) => cells.slice(1, 6))).toEqual([
    ["failed", "KO-229", "human requeue: fixed the fixture", "waited 41m", "operator"],
    ["step", "KO-232", "supervisor kill: no heartbeat", "waited —", "supervisor"],
    ["question", "KO-240", "human resume: the ticket body's", "waited 14m", "operator"],
  ]);
  expect(Array.from(fold.querySelectorAll("[data-resolved-row] [data-kind]")).map((pill) => pill.getAttribute("data-kind"))).toEqual([
    "failed",
    "step",
    "blocked",
  ]);
  expect(shown.every((cells) => /^\d\d:\d\d$/.test(cells[6] ?? ""))).toBe(true);
});

test("with nothing resolved today the strip says 0 and opens to say so; a question older than midnight fetches its thread from when it was asked", async () => {
  const asked: string[] = [];
  const yesterday = MIDNIGHT - min(90);
  const attention: Attention = {
    level: "attention",
    now: NOW,
    items: [{ kind: "blocked", level: "attention", ticket: "KO-240", run: 95, question: "Which path?", asked_ms: yesterday }],
  };
  render(<Now hosts={[hostOf(allKinds.status, attention, BASE)]} project="all" now={NOW} deps={{ fetch: ledgerFetch(attention, [], asked) }} />);
  await act(settle);
  expect(ledgerAsks(asked)).toEqual([windowUrl, threadUrl("KO-240", yesterday)]);
  const fold = screen.getByRole("region", { name: "Resolved today" });
  const strip = within(fold).getByRole("button");
  expect(strip.textContent).toBe("▸Resolved today · 0");
  fireEvent.click(strip);
  expect(within(fold).getByText("Nothing resolved yet today")).toBeTruthy();
});

test("a question's thread is whole when a full page of newer unrelated rows follows it", async () => {
  const older = MIDNIGHT - min(90);
  const attention: Attention = {
    level: "attention",
    now: NOW,
    items: [{ kind: "blocked", level: "attention", ticket: "KO-240", run: 95, question: "Which path?", asked_ms: older }],
  };
  const thread: LedgerRow[] = [
    { at: older + min(1), run: 95, ticket: "KO-240", kind: "note", source: "loop", text: "Parked blocked_on_operator." },
    { at: older + min(12), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: the ticket body's" },
  ];
  const filler: LedgerRow[] = Array.from({ length: 1000 }, (_, i) => ({
    at: MIDNIGHT + i * 1000,
    run: 100 + (i % 7),
    ticket: `KO-${300 + (i % 7)}`,
    kind: "round",
    source: "loop",
    text: `Round ${i}`,
  }));
  render(<Now hosts={[hostOf(allKinds.status, attention, BASE)]} project="all" now={NOW} deps={{ fetch: ledgerFetch(attention, [...thread, ...filler]) }} />);
  await act(settle);
  const first = question("KO-240");
  fireEvent.click(within(first).getByRole("button", { expanded: false }));
  const lines = Array.from(first.querySelectorAll("[data-thread-row]")).map((line) => line.children[1]!.textContent);
  expect(lines).toEqual(["Which path?", "Parked blocked_on_operator.", "human resume: the ticket body's"]);
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

test("Enter on a question row's focused PR link follows the link instead of toggling the thread", () => {
  const url = "https://github.com/o/r/pull/2170";
  const attention = blocked();
  attention.items[0]!.pr_url = url;
  const ledgers: Ledgers = { "writer:7710": { rows: [], threads: { "KO-240": THREAD }, absent: false } };
  render(<NeedsYou hosts={[hostOf(allKinds.status, attention, BASE)]} project="all" now={NOW} ledgers={ledgers} />);
  const first = question("KO-240");
  const toggle = within(first).getByRole("button", { expanded: false });
  const link = within(first).getByText("PR #2170");

  const followed = fireEvent.keyDown(link, { key: "Enter" });
  expect(followed).toBe(true);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(first.querySelector("[data-thread]")).toBeNull();

  fireEvent.keyDown(toggle, { key: "Enter" });
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
});
