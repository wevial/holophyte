import { useEffect, useRef, useState } from "react";
import { defaultPollDeps, fetchJson, type Fetch } from "../lib/poll";
import { groupByDay, medianRounds, mergeRows } from "../lib/shipped";
import type { ShippedBody, ShippedRow } from "../lib/types";
import { ShippedTable } from "./ShippedTable";

/** Rows per `/shipped` page. */
export const SHIPPED_PAGE = 50;

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** One `/shipped` page from `base`; `before` asks for the rows that ended
 *  before that run. */
export function shippedUrl(base: string, limit: number, before?: number): string {
  return `${base}/shipped?limit=${limit}${before == null ? "" : `&before=${before}`}`;
}

/** Whether the daemon has older rows than the page: its `next_before`
 *  when it says, else a full page. */
function hasMore(body: ShippedBody): boolean {
  if ("next_before" in body) return body.next_before != null;
  return body.rows.length >= body.limit;
}

interface Ledger {
  rows: ShippedRow[];
  /** Whether a page older than the oldest row shown is still to fetch. */
  more: boolean;
  /** The most recent fetch's failure, cleared by the next success. */
  error: string | null;
  /** True until the first page answers, good or bad. */
  loading: boolean;
}

const EMPTY: Ledger = { rows: [], more: false, error: null, loading: true };

/** One daemon's rows, each stamped with the daemon it came from so two
 *  daemons' ids never collide in the table. */
const stamp = (base: string, rows: ShippedRow[]): ShippedRow[] => rows.map((row) => ({ ...row, daemon: base }));

/** Every daemon's rows as one ledger, newest end first, ties by id. */
export function concatLedgers(ledgers: Record<string, Ledger>, bases: string[]): ShippedRow[] {
  return bases
    .flatMap((base) => ledgers[base]?.rows ?? [])
    .sort((a, b) => b.ended_ms - a.ended_ms || b.id - a.id);
}

/**
 * The Shipped view: the merge ledger from each daemon's `/shipped`,
 * concatenated and re-sorted newest first under a sub-header per day. One
 * ledger per base: its first page is fetched on mount and again each time
 * `polls` advances (the shell's poll count), merged by id so it stays
 * live; "Load older" fetches, for every daemon with more, the page before
 * the oldest id it has shown. `now` is the clock naming "Today"; `tz` pins
 * the zone for tests.
 */
export function Shipped({
  bases,
  now,
  polls = 0,
  deps = defaultPollDeps,
  tz,
  limit = SHIPPED_PAGE,
}: {
  bases: string[];
  now: number;
  polls?: number;
  deps?: { fetch: Fetch };
  tz?: string;
  limit?: number;
}) {
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const [ledgers, setLedgers] = useState<Record<string, Ledger>>({});
  const [paging, setPaging] = useState(false);
  const key = bases.join("\n");

  const update = (base: string, change: (previous: Ledger) => Ledger) =>
    setLedgers((all) => ({ ...all, [base]: change(all[base] ?? EMPTY) }));

  useEffect(() => {
    let alive = true;
    for (const base of key.split("\n").filter((candidate) => candidate.length > 0)) {
      void (async () => {
        try {
          const body = await fetchJson<ShippedBody>(fetchRef.current, shippedUrl(base, limit));
          if (!alive) return;
          update(base, (previous) => ({
            rows: mergeRows(previous.rows, stamp(base, body.rows)),
            // A refresh only reveals newer rows: the first page's cursor says
            // nothing about pages already fetched, so exhaustion survives it.
            more: previous.rows.length > 0 ? previous.more : hasMore(body),
            error: null,
            loading: false,
          }));
        } catch (failure) {
          if (!alive) return;
          const message = failure instanceof Error ? failure.message : String(failure);
          update(base, (previous) => ({ ...previous, error: message, loading: false }));
        }
      })();
    }
    return () => {
      alive = false;
    };
  }, [key, limit, polls]);

  const loadOlder = async () => {
    if (paging) return;
    setPaging(true);
    await Promise.all(
      bases.map(async (base) => {
        const ledger = ledgers[base];
        if (!ledger?.more) return;
        const oldest = ledger.rows.reduce((least, row) => Math.min(least, row.id), Number.POSITIVE_INFINITY);
        if (!Number.isFinite(oldest)) return;
        try {
          const body = await fetchJson<ShippedBody>(fetchRef.current, shippedUrl(base, limit, oldest));
          update(base, (previous) => ({
            rows: mergeRows(previous.rows, stamp(base, body.rows)),
            more: hasMore(body),
            error: null,
            loading: false,
          }));
        } catch (failure) {
          const message = failure instanceof Error ? failure.message : String(failure);
          update(base, (previous) => ({ ...previous, error: message }));
        }
      }),
    );
    setPaging(false);
  };

  const rows = concatLedgers(ledgers, bases);
  const more = bases.some((base) => ledgers[base]?.more);
  const errors = bases.flatMap((base) => (ledgers[base]?.error ? [ledgers[base]!.error!] : []));
  const loading = bases.some((base) => (ledgers[base] ?? EMPTY).loading);
  const days = groupByDay(rows, now, tz).length;
  const median = medianRounds(rows);
  const subtitle =
    rows.length === 0
      ? null
      : `${plural(rows.length, "merge")} · last ${plural(days, "day")} · median ${median} rounds${more ? ` (of ${rows.length} loaded)` : ""}`;

  return (
    <section aria-label="Shipped" className="px-6 pt-6 pb-6">
      <div className="flex items-baseline gap-3">
        <h1 className="text-[20px] font-semibold text-ink">Shipped</h1>
        {subtitle && (
          <span data-subtitle className="text-[13px] text-muted">
            {subtitle}
          </span>
        )}
      </div>
      {errors.map((error) => (
        <p key={error} role="alert" className="mt-2 font-mono text-[11px] text-bad-text">
          shipped failed: {error}
        </p>
      ))}
      {!loading && rows.length === 0 ? (
        <p className="mt-3 text-[13px] text-muted">Nothing merged yet</p>
      ) : (
        <div className="mt-3">
          <ShippedTable rows={rows} now={now} tz={tz} />
          {more && (
            <button
              type="button"
              disabled={paging}
              onClick={() => void loadOlder()}
              className="mt-3 rounded-button border border-line bg-card px-3 py-1.5 text-[13px] font-semibold text-body hover:bg-hover disabled:opacity-60"
            >
              Load older
            </button>
          )}
        </div>
      )}
    </section>
  );
}
