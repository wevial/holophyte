import { afterEach, expect, test } from "bun:test";
import { act, cleanup, renderHook } from "@testing-library/react";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { runKey } from "../../src/lib/runs";
import type { Attention, Status } from "../../src/lib/types";
import { consoleQueryClient } from "../../src/app/fleet";
import { useReadiness, useSearchAll } from "../../src/app/queries";
import { hostOf, settle } from "../harness";

const WRITER = "http://writer-host:7710";
const PEER = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();
const status = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;

const park = (run: number, park_kind: string) => ({
  kind: "pr_open", level: "attention", ticket: `DEMO-${run}`, run, park_kind, asked_ms: 0,
});
const attentionOf = (...items: Attention["items"]): Attention => ({ level: "attention", now: 0, items });
const hosts = [
  hostOf(status, attentionOf(park(9, "pull_request"), park(8, "ci")), WRITER),
  hostOf(status, attentionOf(park(21, "pull_request")), PEER),
];

/** Answers each `/runs/N/merge` from `answers`, counting every url asked. */
function mergeFetch(answers: Record<string, () => Response>) {
  const asked = new Map<string, number>();
  const fetch: Fetch = async (url) => {
    asked.set(url, (asked.get(url) ?? 0) + 1);
    return answers[url]?.() ?? new Response("not found", { status: 404 });
  };
  return { fetch, asked };
}

const clients: QueryClient[] = [];
afterEach(() => {
  cleanup();
  for (const made of clients.splice(0)) made.clear();
});

async function readinessOf(fetch: Fetch) {
  const queries = consoleQueryClient();
  clients.push(queries);
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queries}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useReadiness(hosts, { fetch }), { wrapper });
  await act(settle);
  await act(settle);
  return { queries, hook };
}

test("readiness reads each pull_request park once across hosts, never a ci park, and drops a failed read", async () => {
  const { fetch, asked } = mergeFetch({
    [`${WRITER}/runs/9/merge`]: () => Response.json({ ...ready, run: 9 }),
    [`${PEER}/runs/21/merge`]: () => new Response("boom", { status: 500 }),
  });
  const { hook } = await readinessOf(fetch);

  expect(Object.fromEntries(asked)).toEqual({ [`${WRITER}/runs/9/merge`]: 1, [`${PEER}/runs/21/merge`]: 1 });
  expect(Object.fromEntries(hook.result.current)).toEqual({ [runKey(addressOf(WRITER), 9)]: { ready: true } });
});

test("a ready run leaves the map when its refetch fails", async () => {
  let answer = () => Response.json({ ...ready, run: 9 });
  const { fetch } = mergeFetch({ [`${WRITER}/runs/9/merge`]: () => answer() });
  const { queries, hook } = await readinessOf(fetch);
  expect(hook.result.current.get(runKey(addressOf(WRITER), 9))).toEqual({ ready: true });

  answer = () => new Response("boom", { status: 500 });
  await act(async () => {
    await queries.refetchQueries();
    await settle();
  });

  expect(hook.result.current.has(runKey(addressOf(WRITER), 9))).toBe(false);
});

test("useSearchAll reads /search?q= from each host and merges its groups, each hit keeping its host; a 404 host adds nothing", async () => {
  const answer = {
    q: "witness", limit: 20,
    tickets: [{ ticket: "DEMO-69", title: "Witness once", status: "merged" }],
    runs: [{ id: 403, ticket: "DEMO-69", title: "Witness once", phase: "done", outcome: "merged", started_ms: 1 }],
    scouts: [{ id: 3, title: "Witness coverage", phase: "done" }],
    findings: [{ run: 411, round: 2, severity: "p1", message: "the witness turn is skipped", path: "holophyte/loop/gates.py", line: 212 }],
  };
  const peerAnswer = { ...answer, tickets: [], scouts: [], findings: [], runs: [{ ...answer.runs[0]!, id: 7 }] };
  const { fetch, asked } = mergeFetch({
    [`${WRITER}/search?q=witness`]: () => Response.json(answer),
    [`${PEER}/search?q=witness`]: () => Response.json(peerAnswer),
    [`${WRITER}/search?q=once`]: () => Response.json({ ...peerAnswer, q: "once" }),
  });
  const queries = consoleQueryClient();
  clients.push(queries);
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queries}>{children}</QueryClientProvider>;
  const hook = renderHook(({ text }) => useSearchAll(hosts, text, { fetch }), { wrapper, initialProps: { text: "witness" } });
  await act(settle);
  await act(settle);

  expect(Object.fromEntries(asked)).toEqual({ [`${WRITER}/search?q=witness`]: 1, [`${PEER}/search?q=witness`]: 1 });
  const found = hook.result.current;
  expect(found.runs.map(({ host, hit }) => [host.base, hit.id])).toEqual([[WRITER, 403], [PEER, 7]]);
  expect(found.tickets.map(({ host, hit }) => [host.base, hit.ticket])).toEqual([[WRITER, "DEMO-69"]]);
  expect(found.scouts.map(({ host }) => host.base)).toEqual([WRITER]);
  expect(found.findings.map(({ host, hit }) => [host.base, hit.run])).toEqual([[WRITER, 411]]);
  expect(found.pending).toBe(false);

  hook.rerender({ text: "once" });
  await act(settle);
  await act(settle);
  const once = hook.result.current;
  expect(asked.get(`${PEER}/search?q=once`)).toBe(1);
  expect(once.runs.map(({ host, hit }) => [host.base, hit.id])).toEqual([[WRITER, 7]]);
  expect([once.tickets, once.scouts, once.findings]).toEqual([[], [], []]);
  expect(once.pending).toBe(false);
  expect(queries.getQueryCache().getAll().every((query) => query.state.error == null)).toBe(true);
});
