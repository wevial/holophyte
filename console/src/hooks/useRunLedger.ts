import type { LedgerRow, RunLedgerBody } from "../lib/ledger";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { useRunResource } from "./useRunResource";

/** One `/runs/N/ledger`: the run's own ledger rows, oldest first. */
export async function fetchRunLedger(base: string, id: number, fetchImpl: Fetch): Promise<LedgerRow[]> {
  const url = `${base}/runs/${id}/ledger`;
  const response = await fetchImpl(url, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`${url} answered ${response.status}`);
  return ((await response.json()) as RunLedgerBody).entries ?? [];
}

/**
 * `/runs/N/ledger` for the expanded run, on the same poll tick as the
 * detail hook. A null `id` — a live run, which has no findings history to
 * show — fetches nothing and answers []. `deps.fetch` is injectable for
 * tests.
 */
export function useRunLedger(
  base: string,
  id: number | null,
  polls: number,
  deps: { fetch: Fetch } = defaultPollDeps,
): LedgerRow[] {
  const { body } = useRunResource(base, id, polls, fetchRunLedger, deps);
  return body ?? [];
}
