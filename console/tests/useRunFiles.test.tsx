import { afterEach, expect, test } from "bun:test";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useRunDetail } from "../src/hooks/useRunDetail";
import { useRunFiles } from "../src/hooks/useRunFiles";
import { usePeers } from "../src/hooks/usePeers";
import type { Fetch } from "../src/lib/poll";
import type { RunDetailBody, RunFilesBody, Status } from "../src/lib/types";
import { NO_ATTENTION, fakeDeps, fixture, settle, stubFetch } from "./harness";

const BASE = "http://writer:7710";
const working = await fixture<Status>("working.json");

afterEach(cleanup);

test("/runs/N/files is requested on expand and again on each poll tick, alongside /runs/N on the same tick", async () => {
  const detail = { run: { id: 91, started_ms: 0, time_box_ms: 1 }, rounds: [], events: [] } as unknown as RunDetailBody;
  const files: RunFilesBody = {
    files: [{ path: "holophyte/serve.py", status: "M", added: 4, deleted: 1 }],
    total_added: 4,
    total_deleted: 1,
  };
  const requests: string[] = [];
  const polling = stubFetch({ status: working, attention: NO_ATTENTION });
  const spy: Fetch = (url, init) => {
    requests.push(url);
    if (url.endsWith("/runs/91/files")) return Promise.resolve(Response.json(files));
    if (url.endsWith("/runs/91")) return Promise.resolve(Response.json(detail));
    return polling(url, init);
  };
  const { deps, clock, firePoll, scheduled } = fakeDeps(spy);
  const count = (path: string) => requests.filter((url) => url === `${BASE}${path}`).length;

  clock.now = 1_000_000;
  const { result, rerender } = renderHook(
    ({ id }: { id: number | null }) => {
      const poll = usePeers(BASE, deps);
      return {
        poll,
        detail: useRunDetail(BASE, id, poll.polls, deps),
        files: useRunFiles(BASE, id, poll.polls, deps),
      };
    },
    { initialProps: { id: null as number | null } },
  );
  await act(settle);
  expect(result.current.poll.polls).toBe(1);
  expect(count("/runs/91/files")).toBe(0);
  expect(result.current.files.loading).toBe(false);

  rerender({ id: 91 });
  expect(result.current.files.loading).toBe(true);
  await act(settle);
  expect(count("/runs/91/files")).toBe(1);
  expect(count("/runs/91")).toBe(1);
  expect(result.current.files).toEqual({ files, error: null, status: null, loading: false });

  clock.now += 10_000;
  await act(async () => {
    firePoll();
    await settle();
  });
  expect(result.current.poll.polls).toBe(2);
  expect(count("/runs/91/files")).toBe(2);
  expect(count("/runs/91")).toBe(2);
  // One poll timer drives all three fetches: nothing else is pending but
  // the next poll and the second-hand tick.
  expect(scheduled.map((entry) => entry.ms).sort((a, b) => a - b)).toEqual([1_000, 10_000]);

  rerender({ id: null });
  expect(result.current.files.files).toBeNull();
  await act(async () => {
    firePoll();
    await settle();
  });
  expect(result.current.poll.polls).toBe(3);
  expect(count("/runs/91/files")).toBe(2);
});

test("404 and 409 carry the daemon's own error text with their status; another status is named by its code", async () => {
  const answer = (status: number, body: BodyInit): Fetch => async () => new Response(body, { status });
  const json = (status: number, error: string) => answer(status, JSON.stringify({ error, run: 7 }));
  const hook = (fetchImpl: Fetch) => renderHook(() => useRunFiles(BASE, 7, 1, { fetch: fetchImpl }));
  const gone = hook(json(409, "branch refs/heads/task/ko-232 cannot be resolved"));
  await act(settle);
  expect(gone.result.current).toEqual({
    files: null,
    error: "branch refs/heads/task/ko-232 cannot be resolved",
    status: 409,
    loading: false,
  });
  const noRange = hook(json(409, "the run recorded neither a branch nor a merge commit"));
  await act(settle);
  expect(noRange.result.current.error).toBe("the run recorded neither a branch nor a merge commit");
  const missing = hook(json(404, "no such run"));
  await act(settle);
  expect(missing.result.current).toEqual({ files: null, error: "no such run", status: 404, loading: false });
  const broken = hook(answer(504, JSON.stringify({ error: "git did not answer within 10s", run: 7 })));
  await act(settle);
  expect(broken.result.current.error).toBe(`${BASE}/runs/7/files answered 504`);
  expect(broken.result.current.status).toBe(504);
  const bodiless = hook(answer(404, "<html>gone</html>"));
  await act(settle);
  expect(bodiless.result.current).toEqual({
    files: null,
    error: `${BASE}/runs/7/files answered 404`,
    status: 404,
    loading: false,
  });
});
