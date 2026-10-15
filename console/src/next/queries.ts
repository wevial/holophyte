import { useQueries, useQuery } from "@tanstack/react-query";
import type { z } from "zod";
import type { ReadinessMap } from "../lib/attention";
import { mergeReadinessSchema, runDetailSchema } from "../lib/contract.gen";
import type { HostRecord } from "../lib/hosts";
import {
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

/** `/runs/N/merge` at `base` as a query, shared by every reader of it. */
export function readinessQuery(base: string, run: number, fetch: Fetch) {
  const path = `/runs/${run}/merge`;
  return {
    queryKey: routeKey(base, path),
    queryFn: () => fetchRoute(fetch, `${base}${path}`, mergeReadinessSchema),
    refetchInterval: READINESS_INTERVAL_MS,
    staleTime: READINESS_INTERVAL_MS,
  };
}

interface Park {
  key: string;
  base: string;
  run: number;
}

function pullRequestParks(hosts: HostRecord<PollBodies>[]): Park[] {
  const parks: Park[] = [];
  for (const host of hosts) {
    for (const item of host.attention?.items ?? []) {
      if (item.kind !== "pr_open" || item.park_kind !== "pull_request") continue;
      if (typeof item.run !== "number" || !Number.isFinite(item.run)) continue;
      parks.push({ key: runKey(host.key, item.run), base: host.base, run: item.run });
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
    queries: parks.map((park) => readinessQuery(park.base, park.run, deps.fetch)),
  });
  const readiness = new Map<string, { ready: boolean }>();
  parks.forEach((park, index) => {
    const result = results[index];
    if (result?.status === "success") readiness.set(park.key, { ready: result.data.ready });
  });
  return readiness;
}

/** `/runs/N` at `base`, read again every `POLL_INTERVAL_MS` while mounted;
 *  idle with no run. */
export function useRun(base: string | null, id: number | null, deps: { fetch: Fetch }) {
  const path = `/runs/${String(id)}`;
  return useQuery({
    queryKey: routeKey(base ?? "", path),
    queryFn: () => fetchRoute(deps.fetch, `${base}${path}`, runDetailSchema),
    enabled: base != null && id != null,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  });
}
