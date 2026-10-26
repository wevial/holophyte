import { useContext } from "react";
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

interface RunPage {
  now: number;
  rows: RunRow[];
  total: number;
  counts: Record<RunFilter, number>;
  settled: boolean;
}

/** One page of the scoped fleet's runs since yesterday's local midnight, in
 *  the asked state and order. One daemon serves the page itself; with
 *  several, each serves its first `page * size` rows and the page is cut
 *  from their merge. */
export function useRunPage({ state, sort, order, page, size }: Required<RunsSearch>): RunPage {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const hosts = scopedHosts(peers.hosts, project);
  const single = hosts.length === 1;
  const query = new URLSearchParams({ since: String(localMidnight(localMidnight(peers.now) - 1)) });
  if (state !== "all") query.set("state", state);
  query.set("sort", sort);
  query.set("order", order);
  const end = page * size;
  const readiness = useReadiness(hosts, deps);
  const { answers, settled } = useHistory(hosts, query.toString(), single ? end - size : 0, end, deps);
  const merged = historyRows(hosts, answers, readiness, peers.now, compareRows(sort, order));
  const counts: Record<RunFilter, number> = { all: 0, active: 0, merged: 0, failed: 0, abandoned: 0 };
  let total = 0;
  for (const answer of answers.values()) {
    total += answer.total;
    for (const group of RUN_GROUPS) counts[group] += answer.counts?.state[group] ?? 0;
  }
  counts.all = state === "all" ? total : RUN_GROUPS.reduce((sum, group) => sum + counts[group], 0);
  return { now: peers.now, rows: single ? merged : merged.slice((page - 1) * size, page * size), total, counts, settled };
}

const RUN_GROUPS = ["active", "merged", "failed", "abandoned"] as const;

export type RunGroup = (typeof RUN_GROUPS)[number];

export type RunFilter = RunGroup | "all";

const RUN_FILTERS: readonly RunFilter[] = ["all", ...RUN_GROUPS];

const GROUP_RANK: Record<RunGroup | "superseded", number> = { active: 0, merged: 1, failed: 2, abandoned: 3, superseded: 4 };

export interface RunsSearch {
  state?: RunFilter;
  sort?: RunSort;
  order?: RunOrder;
  page?: number;
  size?: number;
}

export function validateRunsSearch({ state, sort, order, page, size }: Record<string, unknown>): RunsSearch {
  const pageNumber = Number(page);
  const sizeNumber = Number(size);
  return {
    state: RUN_FILTERS.includes(state as RunFilter) ? (state as RunFilter) : undefined,
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
