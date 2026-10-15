import { afterEach, expect, test } from "bun:test";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { PeersState } from "../../src/hooks/usePeers";
import type { Attention, Status } from "../../src/lib/contract.gen";
import { pollDaemon, type Fetch } from "../../src/lib/poll";
import { consoleQueryClient, useFleet, type ContractBodies } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const ORIGIN = "http://writer-host:7710";
const PEER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const status = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const attention = (await Bun.file(new URL("attention.json", SERVE)).json()) as Attention;

/** `inner`, with every url it was asked for counted. */
function counting(inner: Fetch) {
  const asked = new Map<string, number>();
  const fetch: Fetch = (url, init) => {
    asked.set(url, (asked.get(url) ?? 0) + 1);
    return inner(url, init);
  };
  return { fetch, asked };
}

const clients: QueryClient[] = [];
function client(): QueryClient {
  const made = consoleQueryClient();
  clients.push(made);
  return made;
}

afterEach(() => {
  cleanup();
  for (const made of clients.splice(0)) made.clear();
});

async function fleetOf(fetch: Fetch) {
  const queries = client();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queries}>{children}</QueryClientProvider>;
  const fake = fakeDeps(fetch);
  const hook = renderHook(() => useFleet(ORIGIN, fake.deps), { wrapper });
  await act(settle);
  await act(settle);
  return { queries, hook };
}

const refetch = (queries: QueryClient) => act(async () => {
  await queries.refetchQueries();
  await settle();
});

test("two views reading the fleet share one poll per tick and the same host records", async () => {
  const { fetch, asked } = counting(peersFetch(ORIGIN, { [ORIGIN]: { status, attention } }));
  const queries = client();
  const fake = fakeDeps(fetch);
  const seen: Record<string, PeersState<ContractBodies>> = {};
  function View({ name }: { name: string }) {
    seen[name] = useFleet(ORIGIN, fake.deps);
    return null;
  }
  render(
    <QueryClientProvider client={queries}>
      <View name="sidebar" />
      <View name="switcher" />
    </QueryClientProvider>,
  );
  await act(settle);
  await act(settle);
  await refetch(queries);

  expect(Object.fromEntries(asked)).toEqual({
    [`${ORIGIN}/status`]: 2,
    [`${ORIGIN}/attention`]: 2,
    [`${ORIGIN}/peers`]: 2,
  });
  expect(seen.sidebar!.polls).toBe(2);
  expect(seen.sidebar!.hosts).toHaveLength(1);
  expect(seen.sidebar!.hosts[0]!.status?.host).toBe("writer");
  expect(seen.switcher!.hosts).toBe(seen.sidebar!.hosts);
});

test("a body off the generated contract marks its record with the endpoint and path; the classic schemas still read the status", async () => {
  const { queued: _queued, ...unqueued } = status;
  const laterKind = { ...attention, items: [{ ...attention.items[0], kind: "later_kind" }] };
  const fetch = peersFetch(ORIGIN, {
    [ORIGIN]: { status: unqueued, attention },
    [PEER]: { status, attention: laterKind },
  });
  const { hook } = await fleetOf(fetch);

  const [origin, peer] = hook.result.current.hosts;
  expect(origin).toMatchObject({ contract_error: true, status: null });
  expect(origin!.error).toBe(`Response contract mismatch: ${ORIGIN}/status at queued`);
  expect(peer).toMatchObject({ contract_error: true, status: null });
  expect(peer!.error).toStartWith(`Response contract mismatch: ${PEER}/attention at items.0`);

  const classic = await pollDaemon(ORIGIN, fetch);
  expect(classic.kind).toBe("project");
  expect(classic.kind === "project" && classic.status.project).toBe(status.project);
});

test("a daemon that fails the next poll keeps its last good status beside the new error", async () => {
  let statusAsks = 0;
  const good = peersFetch(ORIGIN, { [ORIGIN]: { status, attention } });
  const fetch: Fetch = async (url, init) => {
    if (url === `${ORIGIN}/status` && ++statusAsks > 1) return new Response("boom", { status: 500 });
    return good(url, init);
  };
  const { queries, hook } = await fleetOf(fetch);
  expect(hook.result.current.hosts[0]).toMatchObject({ status, error: null });

  await refetch(queries);
  const [record] = hook.result.current.hosts;
  expect(record!.status).toEqual(status);
  expect(record!.error).toBe(`${ORIGIN}/status answered 500`);
  expect(hook.result.current.polls).toBe(2);
});

test("the new console's root names the daemon's host once its first poll answers, asking each route once", async () => {
  const { fetch, asked } = counting(peersFetch(ORIGIN, { [ORIGIN]: { status: { ...status, host: "writer-host" }, attention } }));
  const fake = fakeDeps(fetch);
  render(
    <QueryClientProvider client={client()}>
      <NextConsole base={ORIGIN} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  await act(settle);
  await act(settle);

  expect(screen.getByText("writer-host · polled 0s ago")).toBeTruthy();
  expect(Object.fromEntries(asked)).toEqual({
    [`${ORIGIN}/status`]: 1,
    [`${ORIGIN}/attention`]: 1,
    [`${ORIGIN}/peers`]: 1,
  });
});
