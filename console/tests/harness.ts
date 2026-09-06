import { POLL_INTERVAL_MS, type Fetch, type PollDeps } from "../src/lib/poll";
import type { Attention, Status } from "../src/lib/types";

const FIXTURES = new URL("../../tests/fixtures/drawer/", import.meta.url);

/** A drawer fixture, parsed. The bare ones are `/status` bodies; the
 *  `attention_*` ones wrap `status` and `attention` together. */
export async function fixture<T = unknown>(name: string): Promise<T> {
  return (await Bun.file(new URL(name, FIXTURES)).json()) as T;
}

export const NO_ATTENTION: Attention = { level: "none", now: 0, items: [] };

/** A `fetch` answering `/status` and `/attention` from the given bodies. */
export function stubFetch(bodies: { status: Status; attention: Attention }): Fetch {
  return async (url) => {
    if (url.endsWith("/status")) return Response.json(bodies.status);
    if (url.endsWith("/attention")) return Response.json(bodies.attention);
    return new Response("not found", { status: 404 });
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
