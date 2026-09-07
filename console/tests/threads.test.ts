import { expect, test } from "bun:test";
import type { LedgerRow } from "../src/lib/ledger";
import { OPERATOR_LABEL, threadFor } from "../src/lib/threads";

const ASKED = 1_756_890_000_000;

/** KO-240's run 95 as the ledger holds it, newest first as `/ledger`
 *  answers, with another run's row and one older than the question mixed in. */
const LEDGER: LedgerRow[] = [
  { at: ASKED + 900_000, run: 95, ticket: "KO-240", kind: "intervention", source: "operator", text: "human resume: the ticket body's" },
  { at: ASKED + 600_000, run: 88, ticket: "KO-229", kind: "failure", source: "loop", text: "verify failed" },
  { at: ASKED + 60_000, run: 95, ticket: "KO-240", kind: "note", source: "loop", text: "Parked blocked_on_operator." },
  { at: ASKED - 5_000, run: 95, ticket: "KO-240", kind: "round", source: "loop", text: "Round 1: changes_requested" },
];

test("a thread is the question then the run's rows since it was asked, oldest first, with who mapped", () => {
  const item = { kind: "blocked", level: "attention", ticket: "KO-240", run: 95, question: "Which path?", asked_ms: ASKED };
  expect(threadFor(LEDGER, item)).toEqual([
    { who: "run #95", text: "Which path?", at: ASKED },
    { who: "ledger", text: "Parked blocked_on_operator.", at: ASKED + 60_000 },
    { who: OPERATOR_LABEL, text: "human resume: the ticket body's", at: ASKED + 900_000 },
  ]);
});

test("a supervisor's intervention is named as such; an item without a run matches by ticket", () => {
  const rows: LedgerRow[] = [
    { at: 20, run: 7, ticket: "KO-1", kind: "intervention", source: "loop", text: "supervisor kill: time box" },
    { at: 10, run: 7, ticket: "KO-1", kind: "note", source: "loop", text: "parked" },
  ];
  const thread = threadFor(rows, { kind: "blocked", level: "attention", ticket: "KO-1", question: "q" });
  expect(thread.map((row) => row.who)).toEqual(["run", "ledger", "supervisor"]);
  expect(thread[0]!.at).toBeNull();
});
