import { useEffect, useRef, useState } from "react";
import { defaultPollDeps, type Fetch } from "../lib/poll";

/** A daemon answer other than 2xx, keeping the status beside the message
 *  so a caller can tell a named refusal from a passing failure. */
export class AnsweredError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "AnsweredError";
  }
}

export interface RunResourceState<T> {
  /** The last good body for this id, kept through failures. */
  body: T | null;
  /** The most recent fetch's failure, cleared by the next success. */
  error: string | null;
  /** The HTTP status behind `error` when it was an `AnsweredError`, else null. */
  status: number | null;
  /** True until the first answer (good or bad) for this id lands. */
  loading: boolean;
}

interface Inner<T> {
  id: number | null;
  body: T | null;
  error: string | null;
  status: number | null;
}

/**
 * One per-run daemon resource for the expanded run: `load` runs when `id`
 * is set and again each time `polls` advances (the shell's poll count)
 * while it stays set, so an open card rides the one poll timer; a null
 * `id` fetches nothing. `deps.fetch` is injectable for tests.
 */
export function useRunResource<T>(
  base: string,
  id: number | null,
  polls: number,
  load: (base: string, id: number, fetchImpl: Fetch) => Promise<T>,
  deps: { fetch: Fetch } = defaultPollDeps,
): RunResourceState<T> {
  const fetchRef = useRef(deps.fetch);
  fetchRef.current = deps.fetch;
  const loadRef = useRef(load);
  loadRef.current = load;
  const [state, setState] = useState<Inner<T>>({ id: null, body: null, error: null, status: null });

  useEffect(() => {
    if (id == null) return;
    let alive = true;
    void (async () => {
      try {
        const body = await loadRef.current(base, id, fetchRef.current);
        if (alive) setState({ id, body, error: null, status: null });
      } catch (failure) {
        if (!alive) return;
        const message = failure instanceof Error ? failure.message : String(failure);
        const status = failure instanceof AnsweredError ? failure.status : null;
        setState((previous) => ({ id, body: previous.id === id ? previous.body : null, error: message, status }));
      }
    })();
    return () => {
      alive = false;
    };
  }, [base, id, polls]);

  if (id == null || state.id !== id) return { body: null, error: null, status: null, loading: id != null };
  return { body: state.body, error: state.error, status: state.status, loading: false };
}
