import { useQueries } from "@tanstack/react-query";
import type { z } from "zod";
import type { ReadinessMap } from "../lib/attention";
import { ledgerSchema, mergeReadinessSchema, type Ledger, type LedgerEntry } from "../lib/contract.gen";
import type { HostRecord } from "../lib/hosts";
import { LEDGER_LIMIT, localMidnight } from "../lib/ledger";
import {
  AnswerError,
  defaultPollDeps,
  fetchJson,
  MERGE_READINESS_POLLS,
  POLL_INTERVAL_MS,
  REQUEST_TIMEOUT_MS,
  type Fetch,
  type PollBodies,
} from "../lib/poll";
import { runKey } from "../lib/runs";

/** One daemon route's cache key: the base it hangs off and its path. */
export function routeKey(base: string, path: string): readonly ["route", string, string] {
  return ["route", base, path];
}

/** One route's JSON, parsed with `schema` and abandoned at `REQUEST_TIMEOUT_MS`. */
export function fetchRoute<T>(fetch: Fetch, url: string, schema: z.ZodType<T>): Promise<T> {
  return fetchJson(fetch, url, schema, AbortSignal.timeout(REQUEST_TIMEOUT_MS));
}

const READINESS_INTERVAL_MS = MERGE_READINESS_POLLS * POLL_INTERVAL_MS;

interface Park {
  key: string;
  base: string;
  path: string;
}

function pullRequestParks(hosts: HostRecord<PollBodies>[]): Park[] {
  const parks: Park[] = [];
  for (const host of hosts) {
    for (const item of host.attention?.items ?? []) {
      if (item.kind !== "pr_open" || item.park_kind !== "pull_request") continue;
      if (typeof item.run !== "number" || !Number.isFinite(item.run)) continue;
      parks.push({ key: runKey(host.key, item.run), base: host.base, path: `/runs/${item.run}/merge` });
    }
  }
  return parks;
}

/** The merge readiness of every `pull_request` park on `hosts`, one query
 *  each. A park whose latest read failed is left out, so an earlier ready
 *  answer never outlives a refusal. */
export function useReadiness(hosts: HostRecord<PollBodies>[], deps: { fetch: Fetch } = defaultPollDeps): ReadinessMap {
  const parks = pullRequestParks(hosts);
  const results = useQueries({
    queries: parks.map((park) => ({
      queryKey: routeKey(park.base, park.path),
      queryFn: () => fetchRoute(deps.fetch, `${park.base}${park.path}`, mergeReadinessSchema),
      refetchInterval: READINESS_INTERVAL_MS,
      staleTime: READINESS_INTERVAL_MS,
    })),
  });
  const readiness = new Map<string, { ready: boolean }>();
  parks.forEach((park, index) => {
    const result = results[index];
    if (result?.status === "success") readiness.set(park.key, { ready: result.data.ready });
  });
  return readiness;
}

/** A daemon without the route answers 404: no lines, not a failure. */
async function ledgerOrNone(fetch: Fetch, url: string): Promise<Ledger | null> {
  try {
    return await fetchRoute(fetch, url, ledgerSchema);
  } catch (failure) {
    if (failure instanceof AnswerError && failure.status === 404) return null;
    throw failure;
  }
}

/** Every `/ledger` entry of every answering host since local midnight
 *  before `now`, of any kind, so each answer also carries its outages;
 *  newest first. */
export function useDayLedger(hosts: HostRecord<PollBodies>[], now: number, deps: { fetch: Fetch } = defaultPollDeps): LedgerEntry[] {
  const asked = hosts.filter((host) => host.status != null);
  const path = `/ledger?since=${localMidnight(now)}&limit=${LEDGER_LIMIT}`;
  const results = useQueries({
    queries: asked.map((host) => ({
      queryKey: routeKey(host.base, path),
      queryFn: () => ledgerOrNone(deps.fetch, `${host.base}${path}`),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  return results.flatMap((result) => result.data?.entries ?? []).sort((a, b) => b.at - a.at);
}
