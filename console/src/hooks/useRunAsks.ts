import { MERGE_READINESS_POLLS, REQUEST_TIMEOUT_MS, fetchJson, type Fetch } from "../lib/poll";
import { runAsksSchema } from "../lib/schemas";
import type { RunAsks } from "../lib/types";
import { useRunResource } from "./useRunResource";

/** One `/runs/N/asks`, checked at the fetch boundary and abandoned at
 *  `REQUEST_TIMEOUT_MS`. */
function fetchRunAsks(base: string, id: number, fetchImpl: Fetch): Promise<RunAsks> {
  return fetchJson(fetchImpl, `${base}/runs/${id}/asks`, runAsksSchema, AbortSignal.timeout(REQUEST_TIMEOUT_MS));
}

/**
 * The console asks on run `id`'s pull request at `base`, read when `id` is
 * set and again every `MERGE_READINESS_POLLS` of the shell's polls. Null
 * until a read lands and whenever the latest read failed, so a daemon
 * without the route offers no Ask.
 */
export function useRunAsks(
  base: string,
  id: number | null,
  polls: number,
  deps: { fetch: Fetch },
): RunAsks | null {
  const { body, error } = useRunResource(base, id, Math.floor(polls / MERGE_READINESS_POLLS), fetchRunAsks, deps);
  return error == null ? body : null;
}
