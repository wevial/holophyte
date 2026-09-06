import type { Attention, Status } from "./types";

/** The handoff's cadence: one `/status` + `/attention` round trip every 10 s. */
export const POLL_INTERVAL_MS = 10_000;
/** One tick's requests are abandoned at eight seconds so a slow peer never
 *  overlaps the next tick. */
export const REQUEST_TIMEOUT_MS = 8_000;
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

/** One JSON GET; a non-2xx answer throws naming the url and status, and an
 *  aborted one throws "timed out" naming the url. `signal` bounds the wait. */
export async function fetchJson<T>(fetchImpl: Fetch, url: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetchImpl(url, { headers: { accept: "application/json" }, signal });
  } catch (failure) {
    if (failure instanceof Error && (failure.name === "TimeoutError" || failure.name === "AbortError")) {
      throw new Error(`${url} timed out`);
    }
    throw failure;
  }
  if (!response.ok) throw new Error(`${url} answered ${response.status}`);
  return (await response.json()) as T;
}

/** One round trip: both endpoints of the daemon at `base`, both bounded by
 *  `signal` when given. */
export async function pollOnce(base: string, fetchImpl: Fetch, signal?: AbortSignal): Promise<PollAnswer> {
  const [status, attention] = await Promise.all([
    fetchJson<Status>(fetchImpl, `${base}/status`, signal),
    fetchJson<Attention>(fetchImpl, `${base}/attention`, signal),
  ]);
  return { status, attention };
}
