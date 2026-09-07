import { useEffect, useRef, useState } from "react";
import type { HostRecord } from "../lib/hosts";
import {
  evidenceTickets,
  ledgerSince,
  ledgerUrl,
  localMidnight,
  mergeRows,
  ticketLedgerUrl,
  type LedgerBody,
  type LedgerRow,
} from "../lib/ledger";
import { defaultPollDeps, type Fetch } from "../lib/poll";

/** One host's ledger window: its rows, or `absent` for a daemon older
 *  than the endpoint (404), which the band then renders without threads. */
export interface HostLedger {
  rows: LedgerRow[];
  absent: boolean;
}

/** Ledger windows keyed by host address; a host is missing until its
 *  first answer lands. */
export type Ledgers = Record<string, HostLedger>;

/**
 * Each host's `/ledger` window from a day before local midnight (or the oldest open
 * question, when asked earlier), fetched on mount and again each time
 * `polls` advances. A resolution today whose run has no earlier row in the
 * window (`evidenceTickets`) is followed by a fetch of its ticket's whole
 * ledger, merged in, so the wait it ended can still be timed. A 404 marks
 * the host `absent` and stays so until a later poll answers; any other
 * failure keeps the last good rows.
 */
export function useLedger(hosts: HostRecord[], now: number, polls = 0, deps: { fetch: Fetch } = defaultPollDeps): Ledgers {
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const midnightRef = useRef(localMidnight(now));
  midnightRef.current = localMidnight(now);
  const [ledgers, setLedgers] = useState<Ledgers>({});
  const key = hosts
    .filter((host) => host.status != null)
    .map((host) => `${host.address}\t${host.base}\t${ledgerSince(host.attention?.items ?? [], now)}`)
    .join("\n");

  useEffect(() => {
    let alive = true;
    for (const line of key.split("\n").filter((candidate) => candidate.length > 0)) {
      const [address = "", base = "", since = "0"] = line.split("\t");
      void (async () => {
        let next: HostLedger | null = null;
        try {
          const response = await fetchRef.current(ledgerUrl(base, Number(since)), { headers: { accept: "application/json" } });
          if (response.status === 404) next = { rows: [], absent: true };
          else if (response.ok) {
            let rows = ((await response.json()) as LedgerBody).entries ?? [];
            for (const ticket of evidenceTickets(rows, midnightRef.current)) {
              const page = await fetchRef.current(ticketLedgerUrl(base, ticket), { headers: { accept: "application/json" } });
              if (page.ok) rows = mergeRows(rows, ((await page.json()) as LedgerBody).entries ?? []);
            }
            next = { rows, absent: false };
          }
        } catch {
          next = null;
        }
        if (alive && next != null) setLedgers((all) => ({ ...all, [address]: next! }));
      })();
    }
    return () => {
      alive = false;
    };
  }, [key, polls]);

  return ledgers;
}
