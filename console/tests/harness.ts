import { addressOf, type HostRecord } from "../src/lib/hosts";
import { POLL_INTERVAL_MS, type Fetch, type PollDeps } from "../src/lib/poll";
import type { Attention, Status } from "../src/lib/types";

const FIXTURES = new URL("../../tests/fixtures/drawer/", import.meta.url);

/** A drawer fixture, parsed. The bare ones are `/status` bodies; the
 *  `attention_*` ones wrap `status` and `attention` together. */
export async function fixture<T = unknown>(name: string): Promise<T> {
  return (await Bun.file(new URL(name, FIXTURES)).json()) as T;
}

export const NO_ATTENTION: Attention = { level: "none", now: 0, items: [] };

/** A `fetch` answering `/status` and `/attention` from the given bodies,
 *  and `/peers` with no peers. */
export function stubFetch(bodies: { status: Status; attention: Attention }): Fetch {
  return async (url) => {
    if (url.endsWith("/status")) return Response.json(bodies.status);
    if (url.endsWith("/attention")) return Response.json(bodies.attention);
    if (url.endsWith("/peers")) return Response.json({ self: addressOf(url.slice(0, -"/peers".length)), peers: [] });
    return new Response("not found", { status: 404 });
  };
}

/** A daemon a multi-daemon stub answers for: its bodies, or `down` with
 *  the error every request to it rejects with. */
export type StubDaemon = { status: Status; attention: Attention } | { down: Error };

/** A `fetch` for several daemons keyed by base URL: `/peers` from the
 *  origin names every other base, and each base answers its own bodies
 *  or rejects when marked down. */
export function peersFetch(origin: string, daemons: Record<string, StubDaemon>): Fetch {
  return async (url) => {
    const base = Object.keys(daemons).find((candidate) => url.startsWith(`${candidate}/`));
    if (!base) return new Response("not found", { status: 404 });
    const path = url.slice(base.length);
    if (path === "/peers") {
      const peers = Object.keys(daemons)
        .filter((candidate) => candidate !== origin)
        .map(addressOf);
      return Response.json({ self: addressOf(origin), peers });
    }
    const daemon = daemons[base]!;
    if ("down" in daemon) throw daemon.down;
    if (path === "/status") return Response.json(daemon.status);
    if (path === "/attention") return Response.json(daemon.attention);
    return new Response("not found", { status: 404 });
  };
}

/** A host record as one good poll of `base` would leave it. */
export function hostOf(status: Status, attention: Attention, base = "http://writer:7710", polledMs = 0): HostRecord {
  return {
    address: addressOf(base),
    base,
    label: addressOf(base),
    project: status.project ?? status.target,
    status,
    attention,
    polled_ms: polledMs,
    seen_ms: polledMs,
    error: null,
    needs_token: false,
  };
}

/** Poll deps under test control: a settable clock and a timer whose
 *  callbacks are captured so the test fires the next poll by hand. */
export function fakeDeps(fetchImpl: Fetch) {
  const clock = { now: 0 };
  const scheduled: { fn: () => void; ms: number }[] = [];
  const deps: PollDeps = {
    fetch: fetchImpl,
    now: () => clock.now,
    timer: (fn, ms) => {
      const entry = { fn, ms };
      scheduled.push(entry);
      return () => {
        const at = scheduled.indexOf(entry);
        if (at >= 0) scheduled.splice(at, 1);
      };
    },
  };
  /** Run the pending poll callback (the one scheduled at the poll interval). */
  const firePoll = () => {
    const at = scheduled.findIndex((entry) => entry.ms === POLL_INTERVAL_MS);
    if (at < 0) throw new Error("no poll scheduled");
    const [entry] = scheduled.splice(at, 1);
    entry!.fn();
  };
  /** Run the pending second-hand tick. */
  const fireTick = () => {
    const at = scheduled.findIndex((entry) => entry.ms !== POLL_INTERVAL_MS);
    if (at < 0) throw new Error("no tick scheduled");
    const [entry] = scheduled.splice(at, 1);
    entry!.fn();
  };
  return { deps, clock, scheduled, firePoll, fireTick };
}

/** Let the in-flight fetches and their state updates settle. */
export const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

/** Stub `setInterval`/`clearInterval` so a test fires each registered
 *  callback by hand and sees every clear; `restore` puts the real timers
 *  back. Pair with `setSystemTime` to walk the local clock. */
export function captureIntervals() {
  const realSet = globalThis.setInterval;
  const realClear = globalThis.clearInterval;
  const pending = new Map<number, () => void>();
  const cleared: number[] = [];
  let next = 1;
  globalThis.setInterval = ((fn: () => void) => {
    const id = next++;
    pending.set(id, fn);
    return id;
  }) as unknown as typeof setInterval;
  globalThis.clearInterval = ((id: number) => {
    cleared.push(id);
    pending.delete(id);
  }) as unknown as typeof clearInterval;
  const fire = () => {
    for (const fn of [...pending.values()]) fn();
  };
  const restore = () => {
    globalThis.setInterval = realSet;
    globalThis.clearInterval = realClear;
  };
  return { pending, cleared, fire, restore };
}
