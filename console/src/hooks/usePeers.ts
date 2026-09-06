import { useEffect, useRef, useState } from "react";
import { mergeHosts, oldestPoll, peerAddresses, type HostRecord, type PeersBody, type PollResult } from "../lib/hosts";
import {
  POLL_INTERVAL_MS,
  REQUEST_TIMEOUT_MS,
  TICK_MS,
  defaultPollDeps,
  fetchJson,
  pollOnce,
  type PollDeps,
} from "../lib/poll";

export interface PeersState {
  /** The origin first, then each peer `/peers` named, in its order. */
  hosts: HostRecord[];
  /** Milliseconds since the oldest host was last polled, null before the first. */
  polledAgo: number | null;
  /** Ticks completed; a dependent fetch keys off it. */
  polls: number;
  /** The console's clock as of the last tick. */
  now: number;
}

interface Inner {
  hosts: HostRecord[];
  polls: number;
  now: number;
}

const message = (failure: unknown) => (failure instanceof Error ? failure.message : String(failure));

/** One tick: `/peers` from the origin, then `/status` + `/attention` from
 *  the origin and every peer in parallel, each request bounded by
 *  `timeoutMs`. A failed `/peers` keeps polling the addresses last known. */
export async function pollPeers(
  origin: string,
  known: HostRecord[],
  deps: Pick<PollDeps, "fetch">,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<PollResult[]> {
  const signal = AbortSignal.timeout(timeoutMs);
  let addresses: { address: string; base: string }[];
  try {
    addresses = peerAddresses(origin, await fetchJson<PeersBody>(deps.fetch, `${origin}/peers`, signal));
  } catch {
    addresses = known.length > 0 ? known.map(({ address, base }) => ({ address, base })) : peerAddresses(origin, null);
  }
  const settled = await Promise.allSettled(addresses.map(({ base }) => pollOnce(base, deps.fetch, signal)));
  return addresses.map(({ address, base }, index) => {
    const outcome = settled[index]!;
    return outcome.status === "fulfilled"
      ? { address, base, ok: true, ...outcome.value }
      : { address, base, ok: false, error: message(outcome.reason) };
  });
}

/** Fan out to every daemon `/peers` names every `POLL_INTERVAL_MS`, keeping
 *  each host's last good answer through its failures. The one-daemon page
 *  is the `hosts.length === 1` case. `deps` injects fetch, the clock and
 *  the timer; `timeoutMs` bounds each tick's requests. */
export function usePeers(origin: string, deps: PollDeps = defaultPollDeps, timeoutMs = REQUEST_TIMEOUT_MS): PeersState {
  const depsRef = useRef(deps);
  depsRef.current = deps;
  const hostsRef = useRef<HostRecord[]>([]);
  const [state, setState] = useState<Inner>(() => ({ hosts: [], polls: 0, now: deps.now() }));

  useEffect(() => {
    let alive = true;
    let cancel: (() => void) | undefined;
    const run = async () => {
      const { now, timer } = depsRef.current;
      const results = await pollPeers(origin, hostsRef.current, depsRef.current, timeoutMs);
      if (!alive) return;
      const at = now();
      hostsRef.current = mergeHosts(hostsRef.current, results, at);
      setState((previous) => ({ hosts: hostsRef.current, polls: previous.polls + 1, now: at }));
      cancel = timer(run, POLL_INTERVAL_MS);
    };
    void run();
    return () => {
      alive = false;
      cancel?.();
    };
  }, [origin, timeoutMs]);

  useEffect(() => {
    let cancel: (() => void) | undefined;
    const tick = () => {
      setState((previous) => ({ ...previous, now: depsRef.current.now() }));
      cancel = depsRef.current.timer(tick, TICK_MS);
    };
    cancel = depsRef.current.timer(tick, TICK_MS);
    return () => cancel?.();
  }, []);

  const oldest = oldestPoll(state.hosts);
  return {
    hosts: state.hosts,
    polls: state.polls,
    now: state.now,
    polledAgo: oldest == null ? null : Math.max(0, state.now - oldest),
  };
}
