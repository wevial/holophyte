import type { Kind } from "./attention";
import type { LedgerRow } from "./ledger";
import type { AttentionItem } from "./types";

/** One row of the resolved-today fold: the intervention that cleared an
 *  item, with what it cleared and how long that had waited. */
export interface ResolvedRow {
  kind: Kind;
  ticket: string | null;
  run: number | null;
  text: string;
  /** Null when neither the item nor the ledger says when the wait began. */
  waited_ms: number | null;
  /** "operator" for a human, "supervisor" for the loop's own machinery. */
  by: string;
  at: number;
}

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** The action an intervention's text opens with (`human resume: …`,
 *  `supervisor kill: …`), null when the text is not shaped that way. */
function actionOf(text: string): string | null {
  const match = /^(?:human|supervisor) ([a-z_]+):/.exec(text);
  return match ? match[1]! : null;
}

/** When the cleared item's wait began: its `asked_ms` (a question) or
 *  `ended_ms` (a failed run) from the attention item seen for the run,
 *  else the ledger's first row for the run before the resolving one (the
 *  supervisor's strike row for a stale run). */
function waitStart(row: LedgerRow, item: AttentionItem | undefined, rows: LedgerRow[]): number | null {
  const fromItem = item == null ? null : (num(item.asked_ms) ?? num(item.ended_ms));
  if (fromItem != null) return fromItem;
  let first: number | null = null;
  for (const earlier of rows) {
    if (earlier === row || earlier.at >= row.at) continue;
    if (row.run != null ? earlier.run !== row.run : earlier.ticket !== row.ticket) continue;
    if (first == null || earlier.at < first) first = earlier.at;
  }
  return first;
}

/** What an intervention cleared: the item seen for its run, else what the
 *  action says (a kill clears a stale run, a requeue after a failure row
 *  clears a failed run), else a question. */
function kindOf(row: LedgerRow, item: AttentionItem | undefined, rows: LedgerRow[]): Kind {
  if (item?.kind === "blocked" || item?.kind === "stale_run" || item?.kind === "failed" || item?.kind === "supervisor") {
    return item.kind;
  }
  const action = actionOf(row.text);
  if (action === "kill") return "stale_run";
  const failed = rows.some(
    (earlier) => earlier.kind === "failure" && earlier.at <= row.at && earlier.run != null && earlier.run === row.run,
  );
  if (failed && action !== "resume" && action !== "redirect" && action !== "approve") return "failed";
  return "blocked";
}

/** The fold's rows: every `intervention` in the ledger at or after
 *  `midnight`, newest first, each paired with the attention item it
 *  cleared (matched on run, else ticket) for its `waited_ms`. */
export function resolvedSince(rows: LedgerRow[], history: AttentionItem[], midnight: number): ResolvedRow[] {
  return rows
    .filter((row) => row.kind === "intervention" && row.at >= midnight)
    .sort((a, b) => b.at - a.at)
    .map((row) => {
      const item =
        history.find((candidate) => row.run != null && num(candidate.run) === row.run) ??
        history.find((candidate) => row.ticket != null && candidate.ticket === row.ticket);
      const start = waitStart(row, item, rows);
      return {
        kind: kindOf(row, item, rows),
        ticket: row.ticket,
        run: row.run,
        text: row.text,
        waited_ms: start == null ? null : Math.max(0, row.at - start),
        by: row.source === "operator" ? "operator" : "supervisor",
        at: row.at,
      };
    });
}

const waits = (rows: ResolvedRow[]): number[] =>
  rows.map((row) => row.waited_ms).filter((wait): wait is number => wait != null);

/** The median wait among rows that have one; null with none. */
export function median(rows: ResolvedRow[]): number | null {
  const sorted = waits(rows).sort((a, b) => a - b);
  if (sorted.length === 0) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2;
}

/** The longest wait; null with none. */
export function longest(rows: ResolvedRow[]): number | null {
  const all = waits(rows);
  return all.length === 0 ? null : Math.max(...all);
}
