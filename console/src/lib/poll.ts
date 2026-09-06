import { useEffect, useRef, useState } from "react";
import type { Attention, Status } from "./types";

/** The handoff's cadence: one `/status` + `/attention` round trip every 10 s. */
export const POLL_INTERVAL_MS = 10_000;
/** How often `polledAgo` is refreshed between polls. */
export const TICK_MS = 1_000;

/** Schedule `fn` once after `ms`; returns the cancel. `setTimeout` shaped
 *  so tests can drive time by hand. */
export type Timer = (fn: () => void, ms: number) => () => void;

/** The slice of `fetch` the hook uses; tests hand in a stub. */
export type Fetch = (url: string, init?: RequestInit) => Promise<Response>;

export interface PollDeps {
  fetch: Fetch;
  now: () => number;
  timer: Timer;
}

export const defaultPollDeps: PollDeps = {
  fetch: (url, init) => globalThis.fetch(url, init),
  now: () => Date.now(),
  timer: (fn, ms) => {
    const id = setTimeout(fn, ms);
    return () => clearTimeout(id);
  },
};

export interface PollAnswer {
  status: Status;
  attention: Attention;
}

export interface PollState {
  /** The last good `/status`, kept across failures. */
  status: Status | null;
  /** The last good `/attention`, kept across failures. */
  attention: Attention | null;
  /** The most recent poll's failure, cleared by the next success. */
  error: string | null;
  /** Clock reading of the last success, null before the first. */
  lastOkAt: number | null;
  /** Milliseconds since the last success, null before the first. */
  polledAgo: number | null;
}

async function fetchJson<T>(fetchImpl: Fetch, url: string): Promise<T> {
  const response = await fetchImpl(url, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`${url} answered ${response.status}`);
  return (await response.json()) as T;
}

/** One round trip: both endpoints of the daemon at `base`. */
export async function pollOnce(base: string, fetchImpl: Fetch): Promise<PollAnswer> {
  const [status, attention] = await Promise.all([
    fetchJson<Status>(fetchImpl, `${base}/status`),
    fetchJson<Attention>(fetchImpl, `${base}/attention`),
  ]);
  return { status, attention };
}

interface Inner {
  status: Status | null;
  attention: Attention | null;
  error: string | null;
  lastOkAt: number | null;
  now: number;
}

/** Poll the daemon at `base` every `POLL_INTERVAL_MS`, keeping the last good
 *  answer through failures. `deps` injects fetch, the clock and the timer. */
export function usePoll(base: string, deps: PollDeps = defaultPollDeps): PollState {
  const depsRef = useRef(deps);
  depsRef.current = deps;
  const [state, setState] = useState<Inner>(() => ({
    status: null,
    attention: null,
    error: null,
    lastOkAt: null,
    now: deps.now(),
  }));

  useEffect(() => {
    let alive = true;
    let cancel: (() => void) | undefined;
    const run = async () => {
      const { fetch: fetchImpl, now, timer } = depsRef.current;
      try {
        const answer = await pollOnce(base, fetchImpl);
        if (!alive) return;
        const at = now();
        setState({ ...answer, error: null, lastOkAt: at, now: at });
      } catch (failure) {
        if (!alive) return;
        const message = failure instanceof Error ? failure.message : String(failure);
        setState((previous) => ({ ...previous, error: message, now: now() }));
      }
      if (alive) cancel = timer(run, POLL_INTERVAL_MS);
    };
    void run();
    return () => {
      alive = false;
      cancel?.();
    };
  }, [base]);

  useEffect(() => {
    let cancel: (() => void) | undefined;
    const tick = () => {
      setState((previous) => ({ ...previous, now: depsRef.current.now() }));
      cancel = depsRef.current.timer(tick, TICK_MS);
    };
    cancel = depsRef.current.timer(tick, TICK_MS);
    return () => cancel?.();
  }, []);

  const { now, ...rest } = state;
  return {
    ...rest,
    polledAgo: rest.lastOkAt == null ? null : Math.max(0, now - rest.lastOkAt),
  };
}
