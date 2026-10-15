import { useQueries } from "@tanstack/react-query";
import type { ReadinessMap } from "../lib/attention";
import { mergeReadinessSchema } from "../lib/contract.gen";
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
      queryKey: ["route", park.base, park.path],
      queryFn: () => fetchJson(deps.fetch, `${park.base}${park.path}`, mergeReadinessSchema, AbortSignal.timeout(REQUEST_TIMEOUT_MS)),
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
