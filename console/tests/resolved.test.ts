import { expect, test } from "bun:test";
import type { LedgerRow } from "../src/lib/ledger";
import { longest, median, resolvedSince } from "../src/lib/resolved";
import type { AttentionItem } from "../src/lib/types";

const MIDNIGHT = 1_756_857_600_000;
const min = (n: number) => n * 60_000;

const HISTORY: AttentionItem[] = [
  { kind: "blocked", level: "attention", ticket: "KO-240", run: 95, question: "Which path?", asked_ms: MIDNIGHT + min(60) },
  { kind: "failed", level: "attention", ticket: "KO-229", run: 88, reason: "verify failed", ended_ms: MIDNIGHT + min(100) },
];

const LEDGER: LedgerRow[] = [
  { at: MIDNIGHT + min(141), run: 88, ticket: "KO-229", kind: "intervention", source: "operator", text: "human requeue: fixed the fixture" },
  { at: MIDNIGHT + min(131), run: 91, ticket: "KO-232", kind: "intervention", source: "loop", text: "supervisor kill: no heartbeat" },
  { at: MIDNIGHT + min(126), run: 91, ticket: "KO-232", kind: "failure", source: "loop", text: "Strike 1: stale" },
  { at: MIDNIGHT + min(74), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: the ticket body's" },
  { at: MIDNIGHT + min(61), run: 95, ticket: "KO-240", kind: "note", source: "loop", text: "Parked." },
  { at: MIDNIGHT - min(30), run: 50, ticket: "KO-217", kind: "intervention", source: "operator", text: "human resume: yesterday" },
];

test("each intervention since midnight is paired with what it cleared for its wait; median and longest follow", () => {
  const rows = resolvedSince(LEDGER, HISTORY, MIDNIGHT);
  expect(rows.map((row) => [row.kind, row.ticket, row.waited_ms, row.by])).toEqual([
    ["failed", "KO-229", min(41), "operator"],
    ["stale_run", "KO-232", min(5), "supervisor"],
    ["blocked", "KO-240", min(14), "operator"],
  ]);
  expect(rows.map((row) => row.text)).toEqual([
    "human requeue: fixed the fixture",
    "supervisor kill: no heartbeat",
    "human resume: the ticket body's",
  ]);
  expect(median(rows)).toBe(min(14));
  expect(longest(rows)).toBe(min(41));
});

test("an item never seen live falls back to the ledger's first row for the run; nothing since midnight is empty", () => {
  const rows = resolvedSince(LEDGER, [], MIDNIGHT);
  expect(rows.find((row) => row.ticket === "KO-240")?.waited_ms).toBe(min(13));
  expect(rows.find((row) => row.ticket === "KO-229")?.waited_ms).toBeNull();
  expect(resolvedSince(LEDGER, HISTORY, MIDNIGHT + min(200))).toEqual([]);
  expect(median([])).toBeNull();
  expect(longest([])).toBeNull();
});

test("a failure cleared before the console loaded waits from its failure row, not the run's first row", () => {
  // Run 88 reviewed at minute 10, failed at 60 and was requeued at 75: the wait is 15m, not 65m.
  const ledger: LedgerRow[] = [
    { at: MIDNIGHT + min(75), run: 88, ticket: "KO-229", kind: "intervention", source: "operator", text: "human requeue: fixed the fixture" },
    { at: MIDNIGHT + min(60), run: 88, ticket: "KO-229", kind: "failure", source: "loop", text: "verify failed" },
    { at: MIDNIGHT + min(10), run: 88, ticket: "KO-229", kind: "round", source: "loop", text: "Round 1: changes_requested" },
  ];
  const rows = resolvedSince(ledger, [], MIDNIGHT);
  expect(rows.map((row) => [row.kind, row.waited_ms])).toEqual([["failed", min(15)]]);
  expect(median(rows)).toBe(min(15));
  expect(longest(rows)).toBe(min(15));
});
