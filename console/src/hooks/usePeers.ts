import { peerAddresses, rootOf, type HostRecord, type PeersBody, type PollResult } from "../lib/hosts";
import { fetchJson, pollDaemon, pollFailure, type PollBodies, type PollDeps, type PollSchemas } from "../lib/poll";

export interface PeersState<B extends PollBodies> {
  /** The origin first, then each peer `/peers` named, in its order. */
  hosts: HostRecord<B>[];
  /** Milliseconds since the oldest host was last polled, null before the first. */
  polledAgo: number | null;
  /** Ticks completed; a dependent fetch keys off it. */
  polls: number;
  /** The console's clock as of the last tick. */
  now: number;
}

/** One tick: `/status` + `/attention` from every daemon last known (the
 *  origin alone before the first answer) start at once -- and, from a
 *  host daemon, each registered project's own two under its prefix -- beside `GET
 *  /peers` from the origin; a daemon `/peers` newly names is polled as
 *  soon as it is known. Every request of the tick shares one deadline,
 *  `timeoutMs` from its start, so a `/peers` that hangs neither delays
 *  nor pre-empts the known daemons' polls. A failed `/peers` reports the
 *  addresses last known, in their order. Every body is parsed with
 *  `schemas`. */
export async function pollPeers<B extends PollBodies>(
  origin: string,
  known: HostRecord<B>[],
  deps: Pick<PollDeps, "fetch">,
  timeoutMs: number,
  schemas: PollSchemas<B>,
): Promise<PollResult<B>[]> {
  const signal = AbortSignal.timeout(timeoutMs);
  const inFlight = new Map<string, Promise<PollResult<B>>>();
  const start = ({ address, base }: { address: string; base: string }): Promise<PollResult<B>> => {
    let pending = inFlight.get(address);
    if (!pending) {
      pending = pollDaemon(base, deps.fetch, signal, schemas).then(
        (answer): PollResult<B> =>
          answer.kind === "host"
            ? { address, base, ok: true, host: answer.host, host_attention: answer.attention, projects: answer.projects }
            : { address, base, ok: true, status: answer.status, attention: answer.attention },
        (failure: unknown): PollResult<B> => ({ address, base, ...pollFailure(failure) }),
      );
      inFlight.set(address, pending);
    }
    return pending;
  };
  // A host daemon's projects are one address to poll, at the daemon's own base.
  const daemons = new Map(known.map(({ address, base }) => [address, rootOf(base)]));
  let addresses = daemons.size > 0 ? [...daemons].map(([address, base]) => ({ address, base })) : peerAddresses(origin, null);
  addresses.forEach(start);
  try {
    addresses = peerAddresses(origin, await fetchJson<PeersBody>(deps.fetch, `${origin}/peers`, undefined, signal));
  } catch {
    // Discovery failed: the tick reports the addresses it started with.
  }
  return Promise.all(addresses.map(start));
}
