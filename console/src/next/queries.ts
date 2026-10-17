import { useContext } from "react";
import { fetchConfig } from "../lib/config";
import { useQueries, useQuery } from "@tanstack/react-query";
import type { z } from "zod";
import type { ReadinessMap } from "../lib/attention";
import { SHIPPED_PAGE } from "../hooks/useShipped";
import {
  boardSchema,
  findingsSchema,
  historySchema,
  holdsSchema,
  ledgerSchema,
  mergeReadinessSchema,
  peersSchema,
  runAsksSchema,
  runDetailSchema,
  runFilesSchema,
  runTurnsSchema,
  shippedSchema,
  storyDetailSchema,
  ticketDetailSchema,
  type Board,
  type BoardStory,
  type Findings,
  type History,
  type Ledger,
  type LedgerEntry,
  type Peers,
  type StoryDetail,
} from "../lib/contract.gen";
import type { HostRecord } from "../lib/hosts";
import { LEDGER_LIMIT, localMidnight } from "../lib/ledger";
import { projectName } from "../lib/derive";
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
import { PollDepsContext, scopedHosts } from "./scope";
import { shippedUrl, tagRows, todayMerges } from "../lib/shipped";
import type { ShippedRow } from "../lib/types";
import { pastDays, type DayAnswers, type DayWindow } from "./runDays";

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

export function useRunFiles(base: string, id: number, open: boolean, live: boolean, deps: { fetch: Fetch }) {
  const path = `/runs/${id}/files`;
  return useQuery({
    queryKey: [...routeKey(base, path), live],
    queryFn: () => fetchRoute(deps.fetch, `${base}${path}`, runFilesSchema),
    enabled: open,
    refetchInterval: live ? POLL_INTERVAL_MS : false,
    staleTime: POLL_INTERVAL_MS,
  });
}

export function useRunTurns(base: string, id: number, open: boolean, live: boolean, deps: { fetch: Fetch }) {
  const path = `/runs/${id}/turns`;
  return useQuery({
    queryKey: [...routeKey(base, path), live],
    queryFn: () => fetchRoute(deps.fetch, `${base}${path}`, runTurnsSchema),
    enabled: open,
    refetchInterval: live ? POLL_INTERVAL_MS : false,
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

/** `/runs/N/asks` at `base`, read again every `POLL_INTERVAL_MS` while
 *  mounted; null from a daemon without the route, idle with no run. */
export function useRunAsks(base: string | null, run: number | null, deps: { fetch: Fetch }) {
  const path = `/runs/${String(run)}/asks`;
  return useQuery({
    queryKey: routeKey(base ?? "", path),
    queryFn: () => routeOrNone(deps.fetch, `${base}${path}`, runAsksSchema),
    enabled: base != null && run != null,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  });
}

/** `/tickets/KEY` at `base` as a query, read again every `POLL_INTERVAL_MS`
 *  while mounted; null when the daemon has not mirrored it. */
export function ticketQuery(base: string, key: string, fetch: Fetch) {
  const path = `/tickets/${encodeURIComponent(key)}`;
  return {
    queryKey: routeKey(base, path),
    queryFn: () => routeOrNone(fetch, `${base}${path}`, ticketDetailSchema),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  };
}

export function useTicket(base: string, key: string, deps: { fetch: Fetch }) {
  return useQuery(ticketQuery(base, key, deps.fetch));
}

export interface StoryAnswer<R> {
  /** The host whose project bears the asked name, if one does. */
  host: R | null;
  /** The story; null once its daemon answered 404, undefined before any answer. */
  story: StoryDetail | null | undefined;
}

/** `/stories/KEY` at `base`, read again every `POLL_INTERVAL_MS` while
 *  mounted; null once it answered 404, idle with no key. */
export function useStoryAt(base: string | null, key: string | null, deps: { fetch: Fetch }) {
  const path = `/stories/${encodeURIComponent(key ?? "")}`;
  return useQuery({
    queryKey: routeKey(base ?? "", path),
    queryFn: () => routeOrNone(deps.fetch, `${base}${path}`, storyDetailSchema),
    enabled: base != null && key != null,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  });
}

/** `/stories/KEY` of the one host whose project is `project`; idle when no
 *  host serves it. */
export function useStory<R extends HostRecord<PollBodies>>(hosts: R[], project: string, key: string, deps: { fetch: Fetch }): StoryAnswer<R> {
  const host = scopedHosts(hosts, project)[0] ?? null;
  const { data } = useStoryAt(host?.base ?? null, key, deps);
  return { host, story: host == null ? undefined : data };
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
  const answers = useAnswers(hosts, `/ledger?since=${localMidnight(now)}&limit=${LEDGER_LIMIT}`, ledgerSchema, deps);
  const entries = Array.from(answers.values())
    .flatMap((answer) => answer.entries)
    .sort((a, b) => b.at - a.at);
  return { answers, entries };
}

const WINDOW_DAYS = 7;

/** Local midnight `WINDOW_DAYS - 1` days before `now`: today and the six
 *  days before it. */
export function weekSince(now: number): number {
  const day = new Date(localMidnight(now));
  day.setDate(day.getDate() - (WINDOW_DAYS - 1));
  return day.getTime();
}

/** Each scoped host's last good `/findings` answer for the week before
 *  `now`, in fleet order; a host whose daemon lacks the route, or that
 *  never answered, is left out. `answered` is whether any host settled. */
export function useFindings<R extends HostRecord<PollBodies>>(
  hosts: R[],
  now: number,
  deps: { fetch: Fetch } = defaultPollDeps,
): { answered: boolean; answers: { host: R; findings: Findings }[] } {
  const asked = hosts.filter((host) => host.status != null);
  const path = `/findings?since=${weekSince(now)}`;
  const results = useQueries({
    queries: asked.map((host) => ({
      queryKey: routeKey(host.base, path),
      queryFn: () => routeOrNone(deps.fetch, `${host.base}${path}`, findingsSchema),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  const answers = asked.flatMap((host, index) => {
    const findings = results[index]?.data;
    return findings ? [{ host, findings }] : [];
  });
  return { answered: results.some((result) => result.data !== undefined), answers };
}

/** Each status-answering host's last good `path` answer, by its record
 *  `key`; a host whose daemon lacks the route (404) has no entry. */
function useAnswers<T>(hosts: HostRecord<PollBodies>[], path: string, schema: z.ZodType<T>, deps: { fetch: Fetch }): Map<string, T> {
  const asked = hosts.filter((host) => host.status != null);
  const results = useQueries({
    queries: asked.map((host) => ({
      queryKey: routeKey(host.base, path),
      queryFn: () => routeOrNone(deps.fetch, `${host.base}${path}`, schema),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  const answers = new Map<string, T>();
  asked.forEach((host, index) => {
    const data = results[index]?.data;
    if (data) answers.set(host.key, data);
  });
  return answers;
}

/** Each answering host's `/history` since yesterday's local midnight before
 *  `now`, by its record `key`; a host without the route has no entry. */
export function useHistory(hosts: HostRecord<PollBodies>[], now: number, deps: { fetch: Fetch } = defaultPollDeps): Map<string, History> {
  return useAnswers(hosts, `/history?since=${localMidnight(localMidnight(now) - 1)}`, historySchema, deps);
}

function historyDayQuery(base: string, day: DayWindow, limit: number, fetch: Fetch) {
  const path = `/history?since=${day.since}&until=${day.until}&limit=${limit}`;
  return {
    queryKey: routeKey(base, path),
    queryFn: () => routeOrNone(fetch, `${base}${path}`, historySchema),
    staleTime: 60_000,
  };
}

class HistoryAttemptError extends Error {
  constructor(cause: unknown, readonly askedAt: number) {
    super("History query failed", { cause });
  }
}

export function useHistoryDays(hosts: HostRecord<PollBodies>[], days: DayWindow[], deps: { fetch: Fetch; now?: () => number } = defaultPollDeps) {
  const asked = hosts.filter((host) => host.status != null).flatMap((host) => days.map((day) => ({ host, day })));
  const results = useQueries({
    queries: asked.map(({ host, day }) => {
      const query = historyDayQuery(host.base, day, 500, deps.fetch);
      return {
        ...query,
        retry: false,
        refetchOnMount: "always" as const,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        queryFn: async () => {
          const askedAt = (deps.now ?? Date.now)();
          try {
            return await query.queryFn();
          } catch (cause) {
            throw new HistoryAttemptError(cause, askedAt);
          }
        },
      };
    }),
  });
  const answers: DayAnswers = new Map();
  asked.forEach(({ host, day }, index) => {
    const data = results[index]?.data;
    if (!data) return;
    const byHost = answers.get(day.since) ?? new Map<string, History>();
    byHost.set(host.key, data);
    answers.set(day.since, byHost);
  });
  const queries = asked.map(({ host, day }, index) => {
    const result = results[index]!;
    return { host, day, ...result, askedAt: result.error instanceof HistoryAttemptError ? result.error.askedAt : null };
  });
  return { answers, queries };
}

export function useHistoryTotal(hosts: HostRecord<PollBodies>[], now: number, deps: { fetch: Fetch } = defaultPollDeps) {
  const days = pastDays(now);
  const window = { since: days[days.length - 1]!.since, until: days[0]!.until };
  const asked = hosts.filter((host) => host.status != null);
  const results = useQueries({
    queries: asked.map((host) => historyDayQuery(host.base, window, 1, deps.fetch)),
  });
  const complete = asked.length > 0 && asked.length === hosts.length && results.every((result) => result.status === "success" && result.data != null);
  const total = complete ? results.reduce((sum, result) => sum + (result.data?.total ?? 0), 0) : undefined;
  const queries = asked.map((host, index) => ({ host, ...results[index] }));
  return { total, queries };
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

function useBoardResults<R extends HostRecord<PollBodies>>(hosts: R[], deps: { fetch: Fetch }) {
  const asked = hosts.filter((host) => host.status != null);
  const results = useQueries({
    queries: asked.map((host) => ({
      queryKey: routeKey(host.base, "/board"),
      queryFn: () => fetchRoute(deps.fetch, `${host.base}/board`, boardSchema),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  return { asked, results };
}

/** Each host that has answered `/status` with its last good `/board`, in
 *  fleet order; a host whose board never answered is left out. */
export function useBoards<R extends HostRecord<PollBodies>>(hosts: R[], deps: { fetch: Fetch }): { host: R; board: Board }[] {
  const { asked, results } = useBoardResults(hosts, deps);
  return asked.flatMap((host, index) => {
    const board = results[index]?.data;
    return board ? [{ host, board }] : [];
  });
}

export interface ListedStory extends BoardStory {
  project: string;
}

/** Every story on the `/board` of each of `hosts`, with its host's project
 *  name, ordered by key. `answered` is whether every board asked has
 *  answered or failed; a host with no `/status` has no board to wait on. */
export function useBoardStories(hosts: HostRecord<PollBodies>[], deps: { fetch: Fetch }): { answered: boolean; stories: ListedStory[] } {
  const { asked, results } = useBoardResults(hosts, deps);
  const stories = asked.flatMap((host, index) => {
    const board = results[index]?.data;
    const project = host.project;
    return board && project != null ? board.stories.map((story) => ({ ...story, project: projectName(project) })) : [];
  });
  stories.sort((a, b) => a.ticket.localeCompare(b.ticket, undefined, { numeric: true }));
  const answered = asked.every((_, index) => results[index]?.status !== "pending");
  return { answered, stories };
}

export function useConfig(base: string | null) {
  const deps = useContext(PollDepsContext);
  return useQuery({
    queryKey: routeKey(base ?? "", "/config"),
    queryFn: () => fetchConfig(base!, deps.fetch),
    enabled: base != null,
  });
}

export function useHolds(base: string) {
  const deps = useContext(PollDepsContext);
  return useQuery({
    queryKey: routeKey(base, "/holds"),
    queryFn: () => routeOrNone(deps.fetch, `${base}/holds`, holdsSchema),
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  });
}
