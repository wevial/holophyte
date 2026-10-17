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
      return row.phase === "reviewing" ? `${label} · round ${row.rounds + 1} of ${row.max_rounds}` : label;
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

/** Every answer's rows, tagged with their host and project, newest start
 *  first and then by id. */
export function historyRows(hosts: HostRecord<ContractBodies>[], answers: Map<string, History>, readiness: ReadinessMap, now: number): RunRow[] {
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
    .sort((a, b) => b.started_ms - a.started_ms || b.id - a.id);
}

/** The scoped fleet's runs since yesterday's local midnight. */
export function useRunHistory(): { now: number; rows: RunRow[] } {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const hosts = scopedHosts(peers.hosts, project);
  const readiness = useReadiness(hosts, deps);
  const answers = useHistory(hosts, peers.now, deps);
  return { now: peers.now, rows: historyRows(hosts, answers, readiness, peers.now) };
}

const RUN_GROUPS = ["active", "merged", "failed", "abandoned"] as const;

export type RunGroup = (typeof RUN_GROUPS)[number];

export type RunFilter = RunGroup | "all";

const RUN_FILTERS: readonly RunFilter[] = ["all", ...RUN_GROUPS];

export interface RunsSearch {
  state?: RunFilter;
}

export function validateRunsSearch({ state }: Record<string, unknown>): RunsSearch {
  return { state: RUN_FILTERS.includes(state as RunFilter) ? (state as RunFilter) : undefined };
}

export function stateGroup(state: State): RunGroup {
  return state === "merged" || state === "failed" || state === "abandoned" ? state : "active";
}
