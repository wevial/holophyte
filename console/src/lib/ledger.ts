import type { AttentionItem } from "./types";

/** One entry of `GET /ledger` (holophyte/serve.py `ledger()`), as the
 *  daemon spells it: newest first on the wire. An `intervention` entry
 *  from a daemon with KO-308 also says what it `cleared` and how long that
 *  had `waited_ms`; an older daemon leaves both out, which reads as null. */
export interface LedgerRow {
  at: number;
  run: number | null;
  ticket: string | null;
  kind: "merge" | "failure" | "round" | "adjudication" | "intervention" | "note" | string;
  source: "loop" | "operator" | string;
  text: string;
  cleared?: string | null;
  waited_ms?: number | null;
}

export interface LedgerBody {
  entries: LedgerRow[];
  since: number;
  limit: number;
}

/** The daemon's `/runs/N/ledger` body (holophyte/serve.py `run_ledger()`):
 *  one run's entries, oldest first. */
export interface RunLedgerBody {
  run_id: number;
  ticket: string;
  entries: LedgerRow[];
}

/** The endpoint's cap; a day of interventions, or one ticket's rows,
 *  fits in one page, so the console never pages. */
export const LEDGER_LIMIT = 1000;

/** The resolved fold's window: every `intervention` from `since` (local
 *  midnight) on. Asking by kind keeps a busy day's other rows from
 *  crowding the interventions out of the page. */
export function resolvedUrl(base: string, since: number): string {
  return `${base}/ledger?since=${since}&kind=intervention&limit=${LEDGER_LIMIT}`;
}

/** One question's thread: the ledger narrowed to its ticket from the
 *  moment it was asked, as `docs/reference/http.md` spells it. Its own
 *  fetch, so no volume of unrelated newer rows can push the parking note
 *  or the operator's reply past the cap. */
export function threadUrl(base: string, ticket: string, asked: number): string {
  return `${base}/ledger?since=${asked}&ticket=${encodeURIComponent(ticket)}&limit=${LEDGER_LIMIT}`;
}

/** Local midnight before `now`: the start of "today" for the resolved fold. */
export function localMidnight(now: number): number {
  const day = new Date(now);
  day.setHours(0, 0, 0, 0);
  return day.getTime();
}

/** The threads a host's band needs: one per blocked item that names a
 *  ticket, from `asked_ms` (or midnight, for a daemon that sends none). */
export function threadAsks(items: AttentionItem[], midnight: number): { ticket: string; since: number }[] {
  const asks = new Map<string, number>();
  for (const item of items) {
    if (item.kind !== "blocked" || typeof item.ticket !== "string") continue;
    const since = typeof item.asked_ms === "number" ? item.asked_ms : midnight;
    asks.set(item.ticket, Math.min(asks.get(item.ticket) ?? since, since));
  }
  return Array.from(asks, ([ticket, since]) => ({ ticket, since }));
}
