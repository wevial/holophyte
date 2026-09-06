import { useEffect, useRef, useState } from "react";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { RunDetailBody } from "../lib/types";

export interface RunDetailState {
  /** The last good `/runs/N` for this id, kept through failures. */
  detail: RunDetailBody | null;
  /** The most recent fetch's failure, cleared by the next success. */
  error: string | null;
  /** True until the first answer (good or bad) for this id lands. */
  loading: boolean;
}

/** One `/runs/N`; a 404 is named as the store not having the run. */
export async function fetchRunDetail(base: string, id: number, fetchImpl: Fetch): Promise<RunDetailBody> {
  const url = `${base}/runs/${id}`;
  const response = await fetchImpl(url, { headers: { accept: "application/json" } });
  if (response.status === 404) throw new Error(`run ${id} is not in the store`);
  if (!response.ok) throw new Error(`${url} answered ${response.status}`);
  return (await response.json()) as RunDetailBody;
}

interface Inner {
  id: number | null;
  detail: RunDetailBody | null;
  error: string | null;
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
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const [state, setState] = useState<Inner>({ id: null, detail: null, error: null });

  useEffect(() => {
    if (id == null) return;
    let alive = true;
    void (async () => {
      try {
        const detail = await fetchRunDetail(base, id, fetchRef.current);
        if (alive) setState({ id, detail, error: null });
      } catch (failure) {
        if (!alive) return;
        const message = failure instanceof Error ? failure.message : String(failure);
        setState((previous) => ({ id, detail: previous.id === id ? previous.detail : null, error: message }));
      }
    })();
    return () => {
      alive = false;
    };
  }, [base, id, polls]);

  if (id == null || state.id !== id) return { detail: null, error: null, loading: id != null };
  return { detail: state.detail, error: state.error, loading: false };
}
