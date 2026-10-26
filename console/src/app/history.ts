import { useContext, useEffect, useState } from "react";
import { useSearch } from "@tanstack/react-router";
import { describe, type ReadinessMap } from "../lib/attention";
import type { History, HistoryRow, Status } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { formatClock, formatSettled } from "../lib/format";
import { hostItems, sinceSeen, type HostRecord } from "../lib/hosts";
import { localMidnight } from "../lib/ledger";
import { phaseLabel, runKey } from "../lib/runs";
import type { AttentionItem } from "../lib/types";
import type { ContractBodies } from "./fleet";
import { useHistory, useReadiness } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { roundLabel } from "./components/StageChip";
import { STATES, stateOf, type State } from "./state";

interface HostedRun extends HistoryRow {
  base: string;
  project: string;
  state: State;
  seen_ms: number | null;
  thresholds: Status["thresholds"];
}

export interface RunRow extends HostedRun {
  stage: string;
}

const FAILURE_WORDS: Record<string, string> = {
  verify: "failed at verify",
  review_route: "failed at review",
  fix_no_progress: "failed · no progress on a fix",
  no_commits: "failed · no commits",
  budget: "failed · past its time box",
  merge_lock: "failed at merge",
};

export function failureWord(kind: string | null | undefined): string {
  return (kind != null && Object.hasOwn(FAILURE_WORDS, kind) ? FAILURE_WORDS[kind] : undefined) ?? "failed";
}

export function firstLine(text: unknown): string {
  return typeof text === "string" ? (text.split("\n", 1)[0] ?? "").trim() : "";
}

function withDetail(word: string, detail: string | null | undefined, bare = word): string {
  return detail ? `${word} · ${detail}` : bare;
}

/** The state's word with the run's facts after it; `item` is the run's
 *  attention item of the kind its state reads, when its daemon sent one. */
function stageWord(row: HostedRun, item: AttentionItem | null, now: number): string {
  switch (row.state) {
    case "running": {
      const label = phaseLabel(row.phase);
      return row.phase === "reviewing" ? `${label} · ${roundLabel(row.rounds + 1, row.max_rounds)}` : label;
    }
    case "stale":
      return `stale · no beat ${formatSettled((row.heartbeat_age_ms ?? 0) + sinceSeen(row.seen_ms, now))}`;
    case "parked": {
      const facts = item ? (describe(item, row.thresholds, { now }).facts ?? []) : [];
      return withDetail("parked", facts.find((fact) => fact.tone !== "ok")?.label);
    }
    case "question":
      return withDetail("asks", firstLine(item?.question), "asks you");
    case "paused":
      return withDetail("paused", firstLine(item?.note));
    case "resumed":
      return `paused · resumed as run ${row.resumed_as}`;
    case "merged":
      return row.ended_ms != null && row.ended_ms >= localMidnight(now) ? `merged ${formatClock(row.ended_ms)}` : "merged";
    case "failed":
      return failureWord(row.failure_kind);
    case "abandoned":
      return withDetail("abandoned", firstLine(row.outcome_reason));
    default:
      return STATES[row.state].word;
  }
}

const ITEM_KIND: Partial<Record<State, string>> = { parked: "pr_open", question: "blocked", paused: "paused" };

/** Every answer's rows, tagged with their host and project, in the order
 *  `compare` gives. */
export function historyRows(
  hosts: HostRecord<ContractBodies>[], answers: Map<string, History>, readiness: ReadinessMap, now: number, compare: (a: RunRow, b: RunRow) => number,
): RunRow[] {
  return hosts
    .flatMap((host) => {
      const answer = answers.get(host.key);
      const status = host.status;
      if (!answer || !status) return [];
      const staleMs = status.thresholds.heartbeat_stale_ms;
      const project = projectName(status.project);
      const items = hostItems(host, now);
      return answer.rows.map((run) => {
        const state = stateOf({ run, staleMs, readiness: readiness.get(runKey(host.key, run.id)) });
        const kind = ITEM_KIND[state];
        const item = kind ? (items.find((candidate) => candidate.kind === kind && candidate.run === run.id) ?? null) : null;
        const row: HostedRun = { ...run, base: host.base, project, state, seen_ms: host.seen_ms, thresholds: status.thresholds };
        return { ...row, stage: stageWord(row, item, now) };
      });
    })
    .sort(compare);
}

const RUN_SORTS = ["started", "ended", "duration", "rounds", "state"] as const;

export type RunSort = (typeof RUN_SORTS)[number];

export type RunOrder = "asc" | "desc";

export const PAGE_SIZES = [25, 50, 100] as const;

export const DEFAULT_SIZE = 50;

const SORT_KEY: Record<RunSort, (row: RunRow) => number> = {
  started: (row) => row.started_ms,
  ended: (row) => row.ended_ms ?? Number.POSITIVE_INFINITY,
  duration: (row) => row.elapsed_ms,
  rounds: (row) => row.rounds,
  state: (row) => GROUP_RANK[stateGroup(row.state)],
};

/** The daemons' order: the sort key, then newest start, then project and id,
 *  so merging each daemon's first rows keeps every page exact. */
function compareRows(sort: RunSort, order: RunOrder): (a: RunRow, b: RunRow) => number {
  const key = SORT_KEY[sort];
  const sign = order === "asc" ? 1 : -1;
  return (a, b) => {
    const [left, right] = [key(a), key(b)];
    const byKey = left === right ? 0 : left < right ? -sign : sign;
    return byKey || b.started_ms - a.started_ms || a.project.localeCompare(b.project) || b.id - a.id;
  };
}

interface RunCounts {
  state: Record<RunGroup, number>;
  failure: Record<string, number>;
  implementer: Record<string, number>;
}

interface RunPage {
  now: number;
  rows: RunRow[];
  total: number;
  counts: RunCounts;
  implementers: string[];
  projects: { name: string; count: number }[];
  picked: string[];
  settled: boolean;
}

export function listOf(text: string | undefined): string[] {
  return text ? text.split(",") : [];
}

function sumInto(sums: Record<string, number>, counts: Record<string, number> | undefined) {
  for (const [key, count] of Object.entries(counts ?? {})) sums[key] = (sums[key] ?? 0) + count;
}

const RANGE_DAYS_BACK: Record<RunRange, number | null> = { today: 0, "2d": 1, "7d": 6, "30d": 29, any: null };

export function dayStart(day: string): Date {
  const [year, month, date] = day.split("-").map(Number) as [number, number, number];
  return new Date(year, month - 1, date);
}

/** The Started window the search asks for, as `/history`'s `since` and
 *  `until`: `from` and `to` are whole local days and win over `range`. */
function runWindow({ range, from, to }: RunsFilters, now: number): { since: number; until: number | null } {
  if (from != null || to != null) {
    const last = to == null ? null : dayStart(to);
    return {
      since: from == null ? 0 : dayStart(from).getTime(),
      until: last == null ? null : new Date(last.getFullYear(), last.getMonth(), last.getDate() + 1).getTime(),
    };
  }
  const back = RANGE_DAYS_BACK[range ?? DEFAULT_RANGE];
  const today = new Date(now);
  return { since: back == null ? 0 : new Date(today.getFullYear(), today.getMonth(), today.getDate() - back).getTime(), until: null };
}

/** One page of the scoped fleet's runs in the asked Started window, filters
 *  and order. The hosts `projects` names serve the page: one serves it
 *  itself; with several, each serves its first `page * size` rows and the
 *  page is cut from their merge. A host `projects` leaves out is asked for
 *  one row, for its count. */
export function useRunPage(search: Required<RunsSearch> & RunsFilters): RunPage {
  const { state, sort, order, page, size } = search;
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const hosts = scopedHosts(peers.hosts, project);
  const nameOf = (host: HostRecord<ContractBodies>) => (host.status ? projectName(host.status.project) : null);
  const names = hosts.flatMap((host) => nameOf(host) ?? []);
  const picked = listOf(search.projects).filter((name) => names.includes(name));
  const shown = picked.length > 0 ? hosts.filter((host) => picked.includes(nameOf(host) ?? "")) : hosts;
  const left = hosts.filter((host) => !shown.includes(host));
  const single = shown.length === 1;
  const window = runWindow(search, peers.now);
  const query = new URLSearchParams({ since: String(window.since) });
  if (window.until != null) query.set("until", String(window.until));
  if (search.q) query.set("q", search.q);
  const [known, setKnown] = useState<string | null>(null);
  const lists = { state: keptList(state, RUN_GROUPS), failure: keptList(search.failure, FAILURE_KINDS), implementer: keptList(search.implementer, known == null ? undefined : listOf(known)) };
  for (const [name, value] of Object.entries(lists)) if (value) query.set(name, value);
  query.set("sort", sort);
  query.set("order", order);
  const end = page * size;
  const readiness = useReadiness(shown, deps);
  const { answers, settled } = useHistory(shown, query.toString(), single ? end - size : 0, end, deps);
  const others = useHistory(left, query.toString(), 0, 1, deps).answers;
  const merged = historyRows(shown, answers, readiness, peers.now, compareRows(sort, order));
  const counts: RunCounts = { state: { active: 0, merged: 0, failed: 0, abandoned: 0 }, failure: {}, implementer: {} };
  let total = 0;
  for (const answer of answers.values()) total += answer.total;
  const every = [...answers.values(), ...others.values()];
  for (const answer of every) {
    for (const group of RUN_GROUPS) counts.state[group] += answer.counts?.state[group] ?? 0;
    sumInto(counts.failure, answer.counts?.failure);
    sumInto(counts.implementer, answer.counts?.implementer);
  }
  const implementers = [...new Set(every.flatMap((answer) => answer.implementers ?? []))].sort();
  const union = every.length > 0 ? implementers.join(",") : null;
  useEffect(() => { if (union != null) setKnown(union); }, [union]);
  const byName = new Map<string, number>();
  for (const host of hosts) {
    const name = nameOf(host);
    if (name != null) byName.set(name, (byName.get(name) ?? 0) + ((answers.get(host.key) ?? others.get(host.key))?.total ?? 0));
  }
  const projects = [...byName].map(([name, count]) => ({ name, count }));
  return {
    now: peers.now, rows: single ? merged : merged.slice((page - 1) * size, page * size), total, counts, implementers, projects, picked, settled,
  };
}

export const RUN_GROUPS = ["active", "merged", "failed", "abandoned"] as const;

export type RunGroup = (typeof RUN_GROUPS)[number];

export const FAILURE_KINDS = ["verify", "review_route", "fix_no_progress", "no_commits", "budget", "merge_lock", "swept", "infra", "unclassified"] as const;

const RUN_RANGES = ["today", "2d", "7d", "30d", "any"] as const;

export type RunRange = (typeof RUN_RANGES)[number];

export const DEFAULT_RANGE: RunRange = "7d";

const GROUP_RANK: Record<RunGroup | "superseded", number> = { active: 0, merged: 1, failed: 2, abandoned: 3, superseded: 4 };

/** `state` is a comma list of run groups. */
export interface RunsSearch {
  state?: string;
  sort?: RunSort;
  order?: RunOrder;
  page?: number;
  size?: number;
}

/** `projects`, `failure` and `implementer` are comma lists; `from` and `to`
 *  are local `YYYY-MM-DD` days. */
export interface RunsFilters {
  q?: string;
  projects?: string;
  failure?: string;
  implementer?: string;
  range?: RunRange;
  from?: string;
  to?: string;
}

/** `value`'s comma list without blanks, repeats or values outside `allowed`,
 *  in `allowed`'s order when given; undefined when nothing is left. */
function keptList(value: unknown, allowed?: readonly string[]): string | undefined {
  if (typeof value !== "string") return undefined;
  const given = [...new Set(value.split(",").map((each) => each.trim()).filter((each) => each !== ""))];
  const kept = allowed ? allowed.filter((each) => given.includes(each)) : given;
  return kept.length > 0 ? kept.join(",") : undefined;
}

function validDay(value: unknown): string | undefined {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined;
  const day = dayStart(value);
  const written = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
  return written === value ? value : undefined;
}

export function validateRunsSearch(search: Record<string, unknown>): RunsSearch & RunsFilters {
  const { state, sort, order, page, size, q, projects, failure, implementer, range } = search;
  const pageNumber = Number(page);
  const sizeNumber = Number(size);
  const from = validDay(search.from);
  const to = validDay(search.to);
  return {
    q: typeof q === "string" && q.trim() !== "" ? q.trim() : undefined,
    projects: keptList(projects),
    state: keptList(state, RUN_GROUPS),
    failure: keptList(failure, FAILURE_KINDS),
    implementer: keptList(implementer),
    range: range !== DEFAULT_RANGE && RUN_RANGES.includes(range as RunRange) ? (range as RunRange) : undefined,
    from,
    to: to != null && (from == null || to >= from) ? to : undefined,
    sort: RUN_SORTS.includes(sort as RunSort) ? (sort as RunSort) : undefined,
    order: order === "asc" || order === "desc" ? order : undefined,
    page: Number.isSafeInteger(pageNumber) && pageNumber > 1 ? pageNumber : undefined,
    size: PAGE_SIZES.some((each) => each === sizeNumber) ? sizeNumber : undefined,
  };
}

/** A paused run that a later run resumed is "superseded": only All lists it. */
export function stateGroup(state: State): RunGroup | "superseded" {
  if (state === "resumed") return "superseded";
  return state === "merged" || state === "failed" || state === "abandoned" ? state : "active";
}
