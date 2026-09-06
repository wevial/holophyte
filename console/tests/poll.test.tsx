import { afterEach, expect, test } from "bun:test";
import { act, cleanup, renderHook } from "@testing-library/react";
import { POLL_INTERVAL_MS, usePoll, type Fetch } from "../src/lib/poll";
import type { Status } from "../src/lib/types";
import { NO_ATTENTION, fakeDeps, fixture, settle, stubFetch } from "./harness";

const working = await fixture<Status>("working.json");

afterEach(cleanup);

test("a failed poll keeps the last good status, records the error and polledAgo counts from the last success", async () => {
  let calls = 0;
  const good = stubFetch({ status: working, attention: NO_ATTENTION });
  const flaky: Fetch = (url, init) => {
    calls += 1;
    if (calls > 2) return Promise.reject(new Error("connection refused"));
    return good(url, init);
  };
  const { deps, clock, scheduled, firePoll, fireTick } = fakeDeps(flaky);

  clock.now = 1_000_000;
  const { result } = renderHook(() => usePoll("http://writer:7710", deps));
  expect(result.current.polledAgo).toBeNull();
  await act(settle);

  expect(result.current.status).toEqual(working);
  expect(result.current.error).toBeNull();
  expect(result.current.lastOkAt).toBe(1_000_000);
  expect(result.current.polledAgo).toBe(0);
  expect(scheduled.some((entry) => entry.ms === POLL_INTERVAL_MS)).toBe(true);

  clock.now = 1_010_000;
  await act(async () => {
    firePoll();
    await settle();
  });

  expect(result.current.status).toEqual(working);
  expect(result.current.attention).toEqual(NO_ATTENTION);
  expect(result.current.error).toBe("connection refused");
  expect(result.current.lastOkAt).toBe(1_000_000);
  expect(result.current.polledAgo).toBe(10_000);

  clock.now = 1_013_000;
  act(fireTick);
  expect(result.current.polledAgo).toBe(13_000);
});
