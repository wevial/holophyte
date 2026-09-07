import { useEffect, useRef, useState } from "react";
import type { HostRecord } from "../lib/hosts";
import { localMidnight, resolvedUrl, threadAsks, threadUrl, type LedgerBody, type LedgerRow } from "../lib/ledger";
import { defaultPollDeps, type Fetch } from "../lib/poll";

/** One host's ledger: today's interventions for the resolved fold, each
 *  blocked ticket's thread rows by ticket, or `absent` for a daemon older
 *  than the endpoint (404), which the band then renders without threads. */
export interface HostLedger {
  rows: LedgerRow[];
  threads: Record<string, LedgerRow[]>;
  absent: boolean;
}

/** Ledger windows keyed by host address; a host is missing until its
 *  first answer lands. */
export type Ledgers = Record<string, HostLedger>;

/** One `/ledger` page: its rows, `404` for a daemon without the endpoint,
 *  null for any other failure. */
async function page(fetch: Fetch, url: string): Promise<LedgerRow[] | 404 | null> {
  try {
    const response = await fetch(url, { headers: { accept: "application/json" } });
    if (response.status === 404) return 404;
    if (!response.ok) return null;
    return ((await response.json()) as LedgerBody).entries ?? [];
  } catch {
    return null;
  }
}

/** One host's ledger: the resolved window from midnight, then one thread
 *  fetch per blocked ticket from when it was asked. A 404 on the window
 *  marks the host absent; a thread that fails keeps the last one shown. */
async function hostLedger(fetch: Fetch, base: string, midnight: number, asks: { ticket: string; since: number }[], previous?: HostLedger): Promise<HostLedger | null> {
  const rows = await page(fetch, resolvedUrl(base, midnight));
  if (rows === 404) return { rows: [], threads: {}, absent: true };
  if (rows == null) return null;
  const threads: Record<string, LedgerRow[]> = {};
  await Promise.all(
    asks.map(async ({ ticket, since }) => {
      const thread = await page(fetch, threadUrl(base, ticket, since));
      if (Array.isArray(thread)) threads[ticket] = thread;
      else if (previous?.threads[ticket]) threads[ticket] = previous.threads[ticket]!;
    }),
  );
  return { rows, threads, absent: false };
}

/**
 * Each host's `/ledger`: today's interventions from local midnight and,
 * for every blocked ticket on its band, that ticket's rows from
 * `asked_ms` (`?ticket=KO-n&since=ASKED`), fetched on mount and again
 * each time `polls` advances or the band's questions change. A 404 marks
 * the host `absent` and stays so until a later poll answers; any other
 * failure keeps the last good answer.
 */
export function useLedger(hosts: HostRecord[], now: number, polls = 0, deps: { fetch: Fetch } = defaultPollDeps): Ledgers {
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const ledgersRef = useRef<Ledgers>({});
  const [ledgers, setLedgers] = useState<Ledgers>({});
  ledgersRef.current = ledgers;
  const midnight = localMidnight(now);
  const key = hosts
    .filter((host) => host.status != null)
    .map((host) => {
      const asks = threadAsks(host.attention?.items ?? [], midnight)
        .map((ask) => `${ask.ticket}=${ask.since}`)
        .join(",");
      return `${host.address}\t${host.base}\t${asks}`;
    })
    .join("\n");

  useEffect(() => {
    let alive = true;
    for (const line of key.split("\n").filter((candidate) => candidate.length > 0)) {
      const [address = "", base = "", asked = ""] = line.split("\t");
      const asks = asked
        .split(",")
        .filter((ask) => ask.length > 0)
        .map((ask) => {
          const at = ask.lastIndexOf("=");
          return { ticket: ask.slice(0, at), since: Number(ask.slice(at + 1)) };
        });
      void hostLedger(fetchRef.current, base, midnight, asks, ledgersRef.current[address]).then((next) => {
        if (alive && next != null) setLedgers((all) => ({ ...all, [address]: next }));
      });
    }
    return () => {
      alive = false;
    };
  }, [key, midnight, polls]);

  return ledgers;
}
