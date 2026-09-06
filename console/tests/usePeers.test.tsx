import { afterEach, expect, test } from "bun:test";
import { act, cleanup, render } from "@testing-library/react";
import { usePeers, type PeersState } from "../src/hooks/usePeers";
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
