/** One entry of `GET /ledger` (holophyte/serve/server.py `ledger()`), as the
 *  daemon spells it: newest first on the wire. An `intervention` entry
 *  from a daemon with KO-308 also says what it `cleared` and how long that
 *  had `waited_ms`; an older daemon leaves both out, which reads as null. */
export interface LedgerRow {
  at: number;
  action?: string;
  tone?: string;
  project?: number | string;
  schema_to?: number;
  schema_from?: number;
  reason?: string;
  run: number | null;
  ticket: string | null;
  kind: "merge" | "failure" | "round" | "adjudication" | "intervention" | "note" | string;
  source: "loop" | "operator" | string;
  text: string;
  cleared?: string | null;
  waited_ms?: number | null;
}

/** The endpoint's cap; a day of interventions, or one ticket's rows,
 *  fits in one page, so the console never pages. */
export const LEDGER_LIMIT = 1000;

/** Local midnight before `now`: the start of "today" for the resolved fold. */
export function localMidnight(now: number): number {
  const day = new Date(now);
  day.setHours(0, 0, 0, 0);
  return day.getTime();
}
