import { addressOf } from "./hosts";
import { withToken } from "./token";
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

/** `inner` with the stored bearer token for each request's address added
 *  through `withToken`, so every JSON request of the page carries the
 *  token the daemon at that address was given. */
export function tokenedFetch(inner: Fetch): Fetch {
  return (url, init) => inner(url, withToken(addressOf(url), init));
}

export const defaultPollDeps: PollDeps = {
  fetch: tokenedFetch((url, init) => globalThis.fetch(url, init)),
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

/** A non-2xx answer, carrying its status so a 401 can be told apart from
 *  a daemon that is down. */
export class AnswerError extends Error {
  constructor(
    url: string,
    readonly status: number,
  ) {
    super(`${url} answered ${status}`);
    this.name = "AnswerError";
  }
}

/** One JSON GET; a non-2xx answer throws an `AnswerError` naming the url
 *  and status, and an aborted one throws "timed out" naming the url.
 *  `signal` bounds the wait. */
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
  if (!response.ok) throw new AnswerError(url, response.status);
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
