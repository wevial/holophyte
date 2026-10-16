import { useQueries, useQuery } from "@tanstack/react-query";
import type { z } from "zod";
import type { ReadinessMap } from "../lib/attention";
import { SHIPPED_PAGE } from "../hooks/useShipped";
import {
  boardSchema,
  ledgerSchema,
  mergeReadinessSchema,
  peersSchema,
  runDetailSchema,
  shippedSchema,
  ticketDetailSchema,
  type Board,
  type Ledger,
  type LedgerEntry,
  type Peers,
  type TicketDetail,
} from "../lib/contract.gen";
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
import { shippedUrl, tagRows, todayMerges } from "../lib/shipped";
import type { ShippedRow } from "../lib/types";

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

/** A route's 404 is null, not a failure: a daemon without the route, or
 *  without the thing it names. */
async function routeOrNone<T>(fetch: Fetch, url: string, schema: z.ZodType<T>): Promise<T | null> {
  try {
    return await fetchRoute(fetch, url, schema);
  } catch (failure) {
    if (failure instanceof AnswerError && failure.status === 404) return null;
    throw failure;
  }
}

/** `/tickets/KEY` at `base`, read again every `POLL_INTERVAL_MS` while
 *  mounted; null when the daemon has not mirrored it. */
export function useTicket(base: string, key: string, deps: { fetch: Fetch }) {
  const path = `/tickets/${encodeURIComponent(key)}`;
  return useQuery({
    queryKey: routeKey(base, path),
    queryFn: (): Promise<TicketDetail | null> => routeOrNone(deps.fetch, `${base}${path}`, ticketDetailSchema),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  });
}

export interface DayLedger {
  /** Each scoped host's last good `/ledger` answer, by its record `key`. */
  answers: Map<string, Ledger>;
  /** Every host's entries since local midnight, newest first. */
  entries: LedgerEntry[];
}


/** Every `/ledger` entry of every answering host since local midnight
 *  before `now`, of any kind, so each answer also carries its outages. */
export function useDayLedger(hosts: HostRecord<PollBodies>[], now: number, deps: { fetch: Fetch } = defaultPollDeps): DayLedger {
  const asked = hosts.filter((host) => host.status != null);
  const path = `/ledger?since=${localMidnight(now)}&limit=${LEDGER_LIMIT}`;
  const results = useQueries({
    queries: asked.map((host) => ({
      queryKey: routeKey(host.base, path),
      queryFn: (): Promise<Ledger | null> => routeOrNone(deps.fetch, `${host.base}${path}`, ledgerSchema),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  const answers = new Map<string, Ledger>();
  asked.forEach((host, index) => {
    const data = results[index]?.data;
    if (data) answers.set(host.key, data);
  });
  const entries = Array.from(answers.values())
    .flatMap((answer) => answer.entries)
    .sort((a, b) => b.at - a.at);
  return { answers, entries };
}

/** `/peers` at `origin`, read once while `enabled`: the hosts the first
 *  load names before the fleet's whole poll settles. */
export function usePeerList(origin: string, enabled: boolean, deps: { fetch: Fetch }): Peers | null {
  const { data } = useQuery({
    queryKey: routeKey(origin, "/peers"),
    queryFn: () => fetchRoute(deps.fetch, `${origin}/peers`, peersSchema),
    enabled,
    staleTime: Number.POSITIVE_INFINITY,
  });
  return data ?? null;
}

/** Every host's merges since local midnight before `now`, newest first:
 *  one `/shipped` query per host, shared by every page that reads it. */
export function useMergedToday(hosts: HostRecord<PollBodies>[], now: number, fetch: Fetch): ShippedRow[] {
  const path = shippedUrl("", SHIPPED_PAGE, undefined, "merged");
  const results = useQueries({
    queries: hosts.map((host) => ({
      queryKey: routeKey(host.base, path),
      queryFn: () => fetchRoute(fetch, `${host.base}${path}`, shippedSchema),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  const rows = hosts.flatMap((host, index) => {
    const result = results[index];
    return result?.status === "success" ? tagRows(host, result.data.rows) : [];
  });
  return todayMerges(rows, now);
}

/** Each host that has answered `/status` with its last good `/board`, in
 *  fleet order; a host whose board never answered is left out. */
export function useBoards<R extends HostRecord<PollBodies>>(hosts: R[], deps: { fetch: Fetch }): { host: R; board: Board }[] {
  const asked = hosts.filter((host) => host.status != null);
  const results = useQueries({
    queries: asked.map((host) => ({
      queryKey: routeKey(host.base, "/board"),
      queryFn: () => fetchRoute(deps.fetch, `${host.base}/board`, boardSchema),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  return asked.flatMap((host, index) => {
    const board = results[index]?.data;
    return board ? [{ host, board }] : [];
  });
}
