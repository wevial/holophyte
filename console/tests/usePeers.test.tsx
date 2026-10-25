import { expect, test } from "bun:test";
import { z } from "zod";
import { pollPeers } from "../src/hooks/usePeers";
import { mergeHosts } from "../src/lib/hosts";
import type { Fetch, PollSchemas } from "../src/lib/poll";
import type { Attention, HostStatus, Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, peersFetch } from "./harness";

const ORIGIN = "http://writer:7710";
const working = await fixture<Status>("working.json");

const unchecked: PollSchemas<{ status: Status; attention: Attention; host: HostStatus; hostAttention: Attention }> = {
  status: z.custom<Status>(),
  attention: z.custom<Attention>(),
  host: z.custom<HostStatus>(),
  hostAttention: z.custom<Attention>(),
};

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
  const results = await pollPeers(ORIGIN, known, { fetch: abortAware }, 30, unchecked);
  expect(results.map((result) => [result.address, result.ok])).toEqual([
    ["writer:7710", true],
    ["second:7710", true],
  ]);
});
