import { afterEach, expect, test } from "bun:test";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useRunDetail } from "../src/hooks/useRunDetail";
import { usePoll, type Fetch } from "../src/lib/poll";
import type { RunDetailBody, Status } from "../src/lib/types";
import { NO_ATTENTION, fakeDeps, fixture, settle, stubFetch } from "./harness";

const BASE = "http://writer:7710";
const working = await fixture<Status>("working.json");

afterEach(cleanup);

test("/runs/N is fetched once on expand and once per poll while expanded; collapsing stops it", async () => {
  const body = { run: { id: 91, started_ms: 0, time_box_ms: 1 }, rounds: [], events: [] } as unknown as RunDetailBody;
  const requests: string[] = [];
  const polling = stubFetch({ status: working, attention: NO_ATTENTION });
  const spy: Fetch = (url, init) => {
    requests.push(url);
    if (url.endsWith("/runs/91")) return Promise.resolve(Response.json(body));
    return polling(url, init);
  };
  const { deps, clock, firePoll } = fakeDeps(spy);
  const detailRequests = () => requests.filter((url) => url === `${BASE}/runs/91`).length;

  clock.now = 1_000_000;
  const { result, rerender } = renderHook(
    ({ id }: { id: number | null }) => {
      const poll = usePoll(BASE, deps);
      return { poll, detail: useRunDetail(BASE, id, poll.polls, deps) };
    },
    { initialProps: { id: null as number | null } },
  );
  await act(settle);
  expect(result.current.poll.polls).toBe(1);
  expect(detailRequests()).toBe(0);
  expect(result.current.detail.loading).toBe(false);

  rerender({ id: 91 });
  expect(result.current.detail.loading).toBe(true);
  await act(settle);
  expect(detailRequests()).toBe(1);
  expect(result.current.detail.detail).toEqual(body);
  expect(result.current.detail.loading).toBe(false);

  for (const tick of [2, 3]) {
    clock.now += 10_000;
    await act(async () => {
      firePoll();
      await settle();
    });
    expect(result.current.poll.polls).toBe(tick);
    expect(detailRequests()).toBe(tick);
  }

  rerender({ id: null });
  expect(result.current.detail.detail).toBeNull();
  await act(async () => {
    firePoll();
    await settle();
  });
  expect(result.current.poll.polls).toBe(4);
  expect(detailRequests()).toBe(3);
});

test("a failed refresh keeps the last good body and names the failure", async () => {
  const body = { run: { id: 7 }, rounds: [], events: [] } as unknown as RunDetailBody;
  let calls = 0;
  const flaky: Fetch = async () => {
    calls += 1;
    return calls === 1 ? Response.json(body) : new Response("boom", { status: 500 });
  };
  const { result, rerender } = renderHook(({ polls }: { polls: number }) => useRunDetail(BASE, 7, polls, { fetch: flaky }), {
    initialProps: { polls: 1 },
  });
  await act(settle);
  expect(result.current).toEqual({ detail: body, error: null, loading: false });
  rerender({ polls: 2 });
  await act(settle);
  expect(result.current.detail).toEqual(body);
  expect(result.current.error).toBe(`${BASE}/runs/7 answered 500`);
});
