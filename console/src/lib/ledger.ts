import type { AttentionItem } from "./types";

/** One entry of `GET /ledger` (holophyte/serve.py `ledger()`), as the
 *  daemon spells it: newest first on the wire. */
export interface LedgerRow {
  at: number;
  run: number | null;
  ticket: string | null;
  kind: "merge" | "failure" | "round" | "adjudication" | "intervention" | "note" | string;
  source: "loop" | "operator" | string;
  text: string;
}

export interface LedgerBody {
  entries: LedgerRow[];
  since: number;
  limit: number;
}

/** The endpoint's cap; a day of ledger fits in one page, so the console
 *  asks for the whole window at once and never pages. */
export const LEDGER_LIMIT = 1000;

export function ledgerUrl(base: string, since: number): string {
  return `${base}/ledger?since=${since}&limit=${LEDGER_LIMIT}`;
}

/** Local midnight before `now`: the start of "today" for the resolved fold. */
export function localMidnight(now: number): number {
  const day = new Date(now);
  day.setHours(0, 0, 0, 0);
  return day.getTime();
}

/** How far before local midnight the window reaches: an item cleared
 *  early today was asked or failed before midnight, and its wait start is
 *  a row from then, so the fetch runs a day behind the fold's filter. */
export const EVIDENCE_MS = 24 * 60 * 60_000;

/** Where a host's ledger window starts: a day before local midnight, so
 *  the rows behind today's resolutions are in it, or earlier still when a
 *  question on the band was asked before that, so its thread is whole. */
export function ledgerSince(items: AttentionItem[], now: number): number {
  let since = localMidnight(now) - EVIDENCE_MS;
  for (const item of items) {
    if (item.kind === "blocked" && typeof item.asked_ms === "number" && item.asked_ms < since) since = item.asked_ms;
  }
  return since;
}

/** The `/ledger` page for one ticket's whole history: the evidence behind
 *  a resolution whose run predates the window. */
export function ticketLedgerUrl(base: string, ticket: string): string {
  return `${base}/ledger?ticket=${encodeURIComponent(ticket)}&since=0&limit=${LEDGER_LIMIT}`;
}

/** Tickets whose resolution since `midnight` stands alone in the window:
 *  no earlier row for its run is in it, so the wait's start (the parking
 *  note, the failure) is older than the fetch reaches. Each is fetched
 *  whole by ticket and merged in, so the fold can still time the wait. */
export function evidenceTickets(rows: LedgerRow[], midnight: number): string[] {
  const tickets = new Set<string>();
  for (const row of rows) {
    if (row.kind !== "intervention" || row.at < midnight || row.run == null || row.ticket == null) continue;
    const earlier = rows.some((other) => other.run === row.run && other.at < row.at);
    if (!earlier) tickets.add(row.ticket);
  }
  return [...tickets];
}

const rowKey = (row: LedgerRow): string => `${row.at}\t${row.run ?? ""}\t${row.kind}\t${row.source}\t${row.text}`;

/** `rows` with `extra` folded in, newest first, each row once: a ticket's
 *  page overlaps the window on the rows both hold. */
export function mergeRows(rows: LedgerRow[], extra: LedgerRow[]): LedgerRow[] {
  const seen = new Set(rows.map(rowKey));
  const merged = [...rows];
  for (const row of extra) {
    const key = rowKey(row);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(row);
  }
  return merged.sort((a, b) => b.at - a.at);
}
