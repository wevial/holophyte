import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { RunFilesBody } from "../lib/types";
import { useRunResource } from "./useRunResource";

/** The one-line reasons the column shows for the daemon's two named refusals. */
export const FILES_BRANCH_GONE = "branch no longer on disk";
export const FILES_RUN_UNKNOWN = "run not in the store";

export interface RunFilesState {
  /** The last good `/runs/N/files` for this id, kept through failures. */
  files: RunFilesBody | null;
  /** The most recent fetch's failure, cleared by the next success. */
  error: string | null;
  /** True until the first answer (good or bad) for this id lands. */
  loading: boolean;
}

/** One `/runs/N/files`; 404 (unknown run) and 409 (no branch to diff)
 *  are named in the column's words, anything else by its status. */
export async function fetchRunFiles(base: string, id: number, fetchImpl: Fetch): Promise<RunFilesBody> {
  const url = `${base}/runs/${id}/files`;
  const response = await fetchImpl(url, { headers: { accept: "application/json" } });
  if (response.status === 404) throw new Error(FILES_RUN_UNKNOWN);
  if (response.status === 409) throw new Error(FILES_BRANCH_GONE);
  if (!response.ok) throw new Error(`${url} answered ${response.status}`);
  return (await response.json()) as RunFilesBody;
}

/**
 * `/runs/N/files` for the expanded run: fetched when `id` is set and again
 * each time `polls` advances while it stays set, on the same poll tick as
 * the detail hook so an open card never starts a second timer.
 */
export function useRunFiles(
  base: string,
  id: number | null,
  polls: number,
  deps: { fetch: Fetch } = defaultPollDeps,
): RunFilesState {
  const { body, error, loading } = useRunResource(base, id, polls, fetchRunFiles, deps);
  return { files: body, error, loading };
}
