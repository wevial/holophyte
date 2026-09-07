import { useEffect, useRef, useState } from "react";
import { addressOf, mergeHosts, oldestPoll, peerAddresses, type HostRecord, type PeersBody, type PollResult } from "../lib/hosts";
import { forgetToken } from "../lib/token";
import {
  AnswerError,
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

/** One tick: `/status` + `/attention` from every daemon last known (the
 *  origin alone before the first answer) start at once, beside `GET
 *  /peers` from the origin; a daemon `/peers` newly names is polled as
 *  soon as it is known. Every request of the tick shares one deadline,
 *  `timeoutMs` from its start, so a `/peers` that hangs neither delays
 *  nor pre-empts the known daemons' polls. A failed `/peers` reports the
 *  addresses last known, in their order. */
export async function pollPeers(
  origin: string,
  known: HostRecord[],
  deps: Pick<PollDeps, "fetch">,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<PollResult[]> {
  const signal = AbortSignal.timeout(timeoutMs);
  const inFlight = new Map<string, Promise<PollResult>>();
  const start = ({ address, base }: { address: string; base: string }): Promise<PollResult> => {
    let pending = inFlight.get(address);
    if (!pending) {
      pending = pollOnce(base, deps.fetch, signal).then(
        (answer) => ({ address, base, ok: true, ...answer }),
        (failure: unknown) =>
          failure instanceof AnswerError
            ? { address, base, ok: false, error: message(failure), status: failure.status }
            : { address, base, ok: false, error: message(failure) },
      );
      inFlight.set(address, pending);
    }
    return pending;
  };
  let addresses = known.length > 0 ? known.map(({ address, base }) => ({ address, base })) : peerAddresses(origin, null);
  addresses.forEach(start);
  try {
    addresses = peerAddresses(origin, await fetchJson<PeersBody>(deps.fetch, `${origin}/peers`, signal));
  } catch {
    // Discovery failed: the tick reports the addresses it started with.
  }
  return Promise.all(addresses.map(start));
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
    // The next tick is booked when this one starts, so the cadence is the
    // interval itself and a slow daemon (bounded by `timeoutMs`, below the
    // interval) cannot stretch it to interval + timeout.
    const run = async () => {
      const { now, timer } = depsRef.current;
      cancel = timer(run, POLL_INTERVAL_MS);
      const results = await pollPeers(origin, hostsRef.current, depsRef.current, timeoutMs);
      if (!alive) return;
      // A 401 after a stored token means the token is wrong: forget it,
      // once per tick, so the Hosts card asks again rather than the next
      // tick retrying the same value.
      for (const result of results) if (!result.ok && result.status === 401) forgetToken(addressOf(result.base));
      const at = now();
      hostsRef.current = mergeHosts(hostsRef.current, results, at);
      setState((previous) => ({ hosts: hostsRef.current, polls: previous.polls + 1, now: at }));
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
