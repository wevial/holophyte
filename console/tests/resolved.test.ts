import { expect, test } from "bun:test";
import type { LedgerRow } from "../src/lib/ledger";
import { longest, median, resolvedSince } from "../src/lib/resolved";

const MIDNIGHT = 1_756_857_600_000;
const min = (n: number) => n * 60_000;

/** Today's three interventions as KO-308's daemon serves them, newest
 *  first, with a `merge` row and one from before midnight mixed in. */
const LEDGER: LedgerRow[] = [
  { at: MIDNIGHT + min(141), run: 88, ticket: "KO-229", kind: "intervention", source: "operator", text: "human requeue: fixed the fixture", cleared: "failed", waited_ms: 2_460_000 },
  { at: MIDNIGHT + min(131), run: 91, ticket: "KO-232", kind: "intervention", source: "loop", text: "supervisor kill: no heartbeat", cleared: null, waited_ms: null },
  { at: MIDNIGHT + min(126), run: 52, ticket: "KO-219", kind: "merge", source: "loop", text: "MERGED to main as 5acc138." },
  { at: MIDNIGHT + min(74), run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: the ticket body's", cleared: "question", waited_ms: 840_000 },
  { at: MIDNIGHT - min(30), run: 50, ticket: "KO-217", kind: "intervention", source: "operator", text: "human resume: yesterday", cleared: "question", waited_ms: min(5) },
];

test("each intervention since midnight reads its kind and wait from its own entry; median and longest run over the non-null waits", () => {
  const rows = resolvedSince(LEDGER, MIDNIGHT);
  expect(rows.map((row) => [row.kind, row.ticket, row.waited_ms, row.by, row.text])).toEqual([
    ["failed", "KO-229", 2_460_000, "operator", "human requeue: fixed the fixture"],
    ["step", "KO-232", null, "supervisor", "supervisor kill: no heartbeat"],
    ["blocked", "KO-240", 840_000, "operator", "human resume: the ticket body's"],
  ]);
  expect(median(rows)).toBe(1_650_000);
  expect(longest(rows)).toBe(2_460_000);
});

test("a daemon serving entries without the fields yields steps that waited nothing measurable; nothing since midnight is empty", () => {
  const bare: LedgerRow[] = LEDGER.map(({ cleared: _cleared, waited_ms: _waited, ...row }) => row);
  const rows = resolvedSince(bare, MIDNIGHT);
  expect(rows.map((row) => [row.kind, row.waited_ms])).toEqual([["step", null], ["step", null], ["step", null]]);
  expect(median(rows)).toBeNull();
  expect(longest(rows)).toBeNull();
  expect(resolvedSince(LEDGER, MIDNIGHT + min(200))).toEqual([]);
});
