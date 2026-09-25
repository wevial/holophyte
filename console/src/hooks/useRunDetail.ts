import { runDetailSchema } from "../lib/schemas";
import { AnswerError, fetchJson, defaultPollDeps, type Fetch } from "../lib/poll";
import type { RunDetailBody } from "../lib/types";
import { useRunResource } from "./useRunResource";

export interface RunDetailState {
  /** The last good `/runs/N` for this id, kept through failures. */
  detail: RunDetailBody | null;
  /** The most recent fetch's failure, cleared by the next success. */
  error: string | null;
  /** True until the first answer (good or bad) for this id lands. */
  loading: boolean;
}

/** One `/runs/N` of the project at `base`, whose prefix (`/projects/NAME`
 *  on a host daemon) makes run N of two projects two URLs; a 404 is named
 *  as the store not having the run. */
export async function fetchRunDetail(base: string, id: number, fetchImpl: Fetch): Promise<RunDetailBody> {
  const url = `${base}/runs/${id}`;
  try {
    return await fetchJson(fetchImpl, url, runDetailSchema);
  } catch (failure) {
    if (failure instanceof AnswerError && failure.status === 404) throw new Error(`run ${id} is not in the store`);
    throw failure;
  }
}

/**
 * `/runs/N` for the expanded run: fetched when `id` is set and again each
 * time `polls` advances (the Floor's poll count) while it stays set; a
 * null `id` fetches nothing. `deps.fetch` is injectable for tests.
 */
export function useRunDetail(
  base: string,
  id: number | null,
  polls: number,
  deps: { fetch: Fetch } = defaultPollDeps,
): RunDetailState {
  const { body, error, loading } = useRunResource(base, id, polls, fetchRunDetail, deps);
  return { detail: body, error, loading };
}
