import { afterEach, expect, test } from "bun:test";
import { act, cleanup, render } from "@testing-library/react";
import { pollPeers, usePeers, type PeersState } from "../src/hooks/usePeers";
import { mergeHosts } from "../src/lib/hosts";
import { POLL_INTERVAL_MS } from "../src/lib/poll";
import type { Fetch } from "../src/lib/poll";
import type { Status } from "../src/lib/types";
import { NO_ATTENTION, fakeDeps, fixture, peersFetch, settle } from "./harness";

const ORIGIN = "http://writer:7710";
const working = await fixture<Status>("working.json");

afterEach(cleanup);

function Probe({ deps, onState }: { deps: ReturnType<typeof fakeDeps>["deps"]; onState: (s: PeersState) => void }) {
  onState(usePeers(ORIGIN, deps));
  return null;
}

test("the next poll is scheduled from the tick's start, so a slow daemon does not stretch the ten-second cadence", async () => {
  const good = peersFetch(ORIGIN, { [ORIGIN]: { status: working, attention: NO_ATTENTION } });
  let release: (() => void) | undefined;
  const slow: Fetch = (url, init) =>
    url.endsWith("/status")
      ? new Promise<Response>((resolve) => {
          release = () => resolve(good(url, init));
        })
      : good(url, init);
  const { deps, scheduled } = fakeDeps(slow);
  let latest: PeersState | undefined;
  render(<Probe deps={deps} onState={(s) => (latest = s)} />);
  await act(settle);
  // The status answer is still pending, yet the next tick is already on the clock.
  expect(latest?.polls).toBe(0);
  expect(scheduled.filter((entry) => entry.ms === POLL_INTERVAL_MS)).toHaveLength(1);
  release!();
  await act(settle);
  expect(latest?.polls).toBe(1);
  // Completion does not add a second schedule for the same tick.
  expect(scheduled.filter((entry) => entry.ms === POLL_INTERVAL_MS)).toHaveLength(1);
});

test("a /peers that hangs to the deadline does not mark a known, healthy peer unreachable", async () => {
  const PEER = "http://second:7710";
  const good = peersFetch(ORIGIN, {
    [ORIGIN]: { status: working, attention: NO_ATTENTION },
    [PEER]: { status: { ...working, host: "second", project: "/srv/dev/second" }, attention: NO_ATTENTION },
  });
  // Abort-aware: a request whose signal is already aborted, or aborts while
  // pending, rejects the way the browser's fetch does.
  const abortAware: Fetch = (url, init) =>
    new Promise<Response>((resolve, reject) => {
      const signal = init?.signal;
      const fail = () => reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      if (signal?.aborted) return fail();
      signal?.addEventListener("abort", fail, { once: true });
      if (url.endsWith("/peers")) return; // discovery never answers
      good(url, init).then(resolve, reject);
    });
  const known = mergeHosts(
    [],
    [
      { address: "writer:7710", base: ORIGIN, ok: true, status: working, attention: NO_ATTENTION },
      { address: "second:7710", base: PEER, ok: true, status: working, attention: NO_ATTENTION },
    ],
    0,
  );
  const results = await pollPeers(ORIGIN, known, { fetch: abortAware }, 30);
  expect(results.map((result) => [result.address, result.ok])).toEqual([
    ["writer:7710", true],
    ["second:7710", true],
  ]);
});
