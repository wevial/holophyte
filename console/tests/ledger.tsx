import type { HostRecord } from "../src/lib/hosts";
import type { Fetch } from "../src/lib/poll";
import { SHIPPED_PAGE, useShipped } from "../src/hooks/useShipped";
import { Board } from "../src/components/Board";
import { Shipped } from "../src/components/Shipped";

/** The shell's part for a view test: hold the `/shipped` ledger the way
 *  `App` does and hand it to the view. */
interface LedgerProps<H> {
  hosts: H[];
  now: number;
  polls?: number;
  deps: { fetch: Fetch };
  tz?: string;
  limit?: number;
}

export function ShippedWithLedger({ hosts, now, polls = 0, deps, tz, limit = SHIPPED_PAGE }: LedgerProps<Pick<HostRecord, "base" | "project">>) {
  const shipped = useShipped(hosts, polls, deps, limit);
  return <Shipped shipped={shipped} now={now} tz={tz} />;
}

export function BoardWithLedger({ hosts, now, polls = 0, deps, tz, limit = SHIPPED_PAGE }: LedgerProps<HostRecord>) {
  const shipped = useShipped(hosts, polls, deps, limit);
  return <Board hosts={hosts} shipped={shipped} now={now} polls={polls} deps={deps} tz={tz} />;
}
