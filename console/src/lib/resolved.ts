import { PILL_TEXT, type Kind } from "./attention";
import type { LedgerRow } from "./ledger";

/** What a fold row's pill says: a band kind for a resolution the daemon
 *  classified, `step` for one it did not (or could not, being older than
 *  KO-308). */
export type ResolvedKind = Kind | "step";

/** One row of the resolved-today fold: the intervention that cleared an
 *  item, with what it cleared and how long that had waited, both as the
 *  entry itself says (KO-308). */
export interface ResolvedRow {
  kind: ResolvedKind;
  ticket: string | null;
  run: number | null;
  text: string;
  /** Null when the entry carries no `waited_ms`. */
  waited_ms: number | null;
  /** "operator" for a human, "supervisor" for the loop's own machinery. */
  by: string;
  at: number;
}

/** The entry's `cleared` as a pill kind; anything else is a neutral step. */
const CLEARED_KIND: Record<string, ResolvedKind> = {
  question: "blocked",
  stale_run: "stale_run",
  failed: "failed",
  supervisor: "supervisor",
};

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** The fold rows: every `intervention` in `rows` at or after `midnight`,
 *  newest first, each with the wait and the kind its own entry carries.
 *  Nothing is paired against the band or other runs: an entry without the
 *  fields is a `step` that "waited —". */
export function resolvedSince(rows: LedgerRow[], midnight: number): ResolvedRow[] {
  return rows
    .filter((row) => row.kind === "intervention" && row.at >= midnight)
    .sort((a, b) => b.at - a.at)
    .map((row) => ({
      kind: (row.cleared != null && CLEARED_KIND[row.cleared]) || "step",
      ticket: row.ticket,
      run: row.run,
      text: row.text,
      waited_ms: num(row.waited_ms),
      by: row.source === "operator" ? "operator" : "supervisor",
      at: row.at,
    }));
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

/** The pill's text for a fold row. */
export function pillText(kind: ResolvedKind): string {
  return kind === "step" ? "step" : PILL_TEXT[kind];
}
