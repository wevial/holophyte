import { useEffect, useRef, useState } from "react";
import type { HostRecord } from "../lib/hosts";
import { defaultPollDeps, fetchJson, type Fetch } from "../lib/poll";
import { mergeRows, tagRows } from "../lib/shipped";
import type { ShippedBody, ShippedRow } from "../lib/types";

/** Rows per `/shipped` page. */
export const SHIPPED_PAGE = 50;

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

/** Every daemon's rows as one ledger, newest end first, ties by id. */
export function concatLedgers(ledgers: Record<string, Ledger>, bases: string[]): ShippedRow[] {
  return bases
    .flatMap((base) => ledgers[base]?.rows ?? [])
    .sort((a, b) => b.ended_ms - a.ended_ms || b.id - a.id);
}

export interface ShippedState {
  /** Every host's rows as one ledger, newest first. */
  rows: ShippedRow[];
  /** Whether any host has a page older than what it has shown. */
  more: boolean;
  /** Each host's most recent failure, in host order. */
  errors: string[];
  /** True until every host's first page has answered, good or bad. */
  loading: boolean;
  /** True while "Load older" is in flight. */
  paging: boolean;
  /** Fetch, for every host with more, the page before the oldest id it has shown. */
  loadOlder: () => Promise<void>;
}

/**
 * The merge ledger from each host's `/shipped`, held once for the Shipped
 * view and the Board's Shipped-today table. One ledger per base: its first
 * page is fetched on mount and again each time `polls` advances (the
 * shell's poll count), merged by id so it stays live. Each row is tagged
 * with its daemon's project name (`tagRows`) for the Project column.
 */
export function useShipped(
  hosts: Pick<HostRecord, "base" | "project">[],
  polls = 0,
  deps: { fetch: Fetch } = defaultPollDeps,
  limit = SHIPPED_PAGE,
): ShippedState {
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const [ledgers, setLedgers] = useState<Record<string, Ledger>>({});
  const [paging, setPaging] = useState(false);
  const bases = hosts.map((host) => host.base);
  // The project is part of the key: a host whose `/status` first names it
  // after the page mounts gets its rows fetched, and stamped, again.
  const key = hosts.map((host) => `${host.base}\t${host.project ?? ""}`).join("\n");

  const update = (base: string, change: (previous: Ledger) => Ledger) =>
    setLedgers((all) => ({ ...all, [base]: change(all[base] ?? EMPTY) }));

  useEffect(() => {
    let alive = true;
    for (const line of key.split("\n").filter((candidate) => candidate.length > 0)) {
      const [base = "", project = ""] = line.split("\t");
      void (async () => {
        try {
          const body = await fetchJson<ShippedBody>(fetchRef.current, shippedUrl(base, limit));
          if (!alive) return;
          update(base, (previous) => ({
            rows: mergeRows(previous.rows, tagRows({ base, project: project || null }, body.rows)),
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
      hosts.map(async (host) => {
        const { base } = host;
        const ledger = ledgers[base];
        if (!ledger?.more) return;
        const oldest = ledger.rows.reduce((least, row) => Math.min(least, row.id), Number.POSITIVE_INFINITY);
        if (!Number.isFinite(oldest)) return;
        try {
          const body = await fetchJson<ShippedBody>(fetchRef.current, shippedUrl(base, limit, oldest));
          update(base, (previous) => ({
            rows: mergeRows(previous.rows, tagRows(host, body.rows)),
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

  return {
    rows: concatLedgers(ledgers, bases),
    more: bases.some((base) => ledgers[base]?.more),
    errors: bases.flatMap((base) => (ledgers[base]?.error ? [ledgers[base]!.error!] : [])),
    loading: bases.some((base) => (ledgers[base] ?? EMPTY).loading),
    paging,
    loadOlder,
  };
}
