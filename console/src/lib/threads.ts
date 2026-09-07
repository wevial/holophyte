import type { LedgerRow } from "./ledger";
import type { AttentionItem } from "./types";

/** What the who column calls a human's ledger row: the wire carries no
 *  name, only `source: operator`. */
export const OPERATOR_LABEL = "operator";

/** One line of a question thread: who said it, what, and when (null for a
 *  question whose asking time the daemon did not send). */
export interface ThreadRow {
  who: string;
  text: string;
  at: number | null;
}

/** The who column for a ledger row: a human is the operator, the
 *  supervisor's own intervention says so, and the loop's other rows (the
 *  parking note, a round) are the ledger's. */
export function whoOf(row: Pick<LedgerRow, "kind" | "source">): string {
  if (row.source === "operator") return OPERATOR_LABEL;
  if (row.kind === "intervention") return "supervisor";
  return "ledger";
}

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** The rows of `item`'s blocked run, oldest first: the question itself as
 *  run #N asked it, then every ledger row for that run (by ticket when the
 *  item names no run) from `asked_ms` on. */
export function threadFor(rows: LedgerRow[], item: AttentionItem): ThreadRow[] {
  const run = num(item.run);
  const ticket = typeof item.ticket === "string" ? item.ticket : null;
  const asked = num(item.asked_ms);
  const ledger = rows
    .filter((row) => (run != null ? row.run === run : row.ticket === ticket))
    .filter((row) => asked == null || row.at >= asked)
    .sort((a, b) => a.at - b.at)
    .map((row) => ({ who: whoOf(row), text: row.text, at: row.at }));
  const question: ThreadRow = {
    who: run == null ? "run" : `run #${run}`,
    text: typeof item.question === "string" ? item.question : "",
    at: asked,
  };
  return [question, ...ledger];
}
