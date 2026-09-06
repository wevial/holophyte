import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { RunFilesBody } from "../lib/types";
import { AnsweredError, useRunResource } from "./useRunResource";

export interface RunFilesState {
  /** The last good `/runs/N/files` for this id, kept through failures. */
  files: RunFilesBody | null;
  /** The most recent fetch's failure, cleared by the next success. */
  error: string | null;
  /** The HTTP status behind `error` when the daemon answered at all. */
  status: number | null;
  /** True until the first answer (good or bad) for this id lands. */
  loading: boolean;
}

/** The `error` line of a daemon refusal's JSON body, or nothing when the
 *  body is not that shape (a proxy's HTML, an empty answer). */
async function refusalMessage(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string") {
      return (body as { error: string }).error;
    }
  } catch {
    // Not JSON: fall through to the status line.
  }
  return null;
}

/** One `/runs/N/files`. A failure is an `AnsweredError` carrying the
 *  status: for the daemon's named refusals (404 unknown run, 409 no range
 *  to diff) the message is the body's own `error` text, anything else is
 *  named by its status. */
export async function fetchRunFiles(base: string, id: number, fetchImpl: Fetch): Promise<RunFilesBody> {
  const url = `${base}/runs/${id}/files`;
  const response = await fetchImpl(url, { headers: { accept: "application/json" } });
  if (response.ok) return (await response.json()) as RunFilesBody;
  const message = response.status === 404 || response.status === 409 ? await refusalMessage(response) : null;
  throw new AnsweredError(response.status, message ?? `${url} answered ${response.status}`);
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
  const { body, error, status, loading } = useRunResource(base, id, polls, fetchRunFiles, deps);
  return { files: body, error, status, loading };
}
