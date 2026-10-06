import { MERGE_READINESS_POLLS, REQUEST_TIMEOUT_MS, fetchJson, type Fetch } from "../lib/poll";
import { mergeReadinessSchema } from "../lib/schemas";
import type { MergeReadiness } from "../lib/types";
import { useRunResource } from "./useRunResource";

/** One `/runs/N/merge`, checked at the fetch boundary and abandoned at
 *  `REQUEST_TIMEOUT_MS`: a read that hangs on GitHub must fail, not leave
 *  the last ready answer drawn. */
function fetchMergeReadiness(base: string, id: number, fetchImpl: Fetch): Promise<MergeReadiness> {
  return fetchJson(fetchImpl, `${base}/runs/${id}/merge`, mergeReadinessSchema, AbortSignal.timeout(REQUEST_TIMEOUT_MS));
}

/**
 * The merge readiness of run `id` at `base`, read when `id` is set and
 * again every `MERGE_READINESS_POLLS` of the shell's polls. Null until a
 * read lands and whenever the latest read failed (a refusal, a timeout, a
 * body outside the contract), so a stale answer never draws a button.
 */
export function useMergeReadiness(
  base: string,
  id: number | null,
  polls: number,
  deps: { fetch: Fetch },
): MergeReadiness | null {
  const { body, error } = useRunResource(base, id, Math.floor(polls / MERGE_READINESS_POLLS), fetchMergeReadiness, deps);
  return error == null ? body : null;
}
