import { useContext } from "react";
import { useSearch } from "@tanstack/react-router";
import type { ReadinessMap } from "../lib/attention";
import type { History, HistoryRow } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import type { HostRecord } from "../lib/hosts";
import { runKey } from "../lib/runs";
import type { ContractBodies } from "./fleet";
import { useHistory, useReadiness } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { stateOf, type State } from "./state";

export interface RunRow extends HistoryRow {
  base: string;
  project: string;
  state: State;
}

/** Every answer's rows, tagged with their host and project, newest start
 *  first and then by id. */
export function historyRows(hosts: HostRecord<ContractBodies>[], answers: Map<string, History>, readiness: ReadinessMap): RunRow[] {
  return hosts
    .flatMap((host) => {
      const answer = answers.get(host.key);
      if (!answer || !host.status) return [];
      const staleMs = host.status.thresholds.heartbeat_stale_ms;
      const project = projectName(host.status.project);
      return answer.rows.map((run) => ({
        ...run,
        base: host.base,
        project,
        state: stateOf({ run, staleMs, readiness: readiness.get(runKey(host.key, run.id)) }),
      }));
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
  return { now: peers.now, rows: historyRows(hosts, answers, readiness) };
}

const RUN_GROUPS = ["active", "merged", "failed", "abandoned"] as const;

export type RunGroup = (typeof RUN_GROUPS)[number];

export interface RunsSearch {
  state?: RunGroup;
}

export function validateRunsSearch({ state }: Record<string, unknown>): RunsSearch {
  return { state: RUN_GROUPS.includes(state as RunGroup) ? (state as RunGroup) : undefined };
}

export function stateGroup(state: State): RunGroup {
  return state === "merged" || state === "failed" || state === "abandoned" ? state : "active";
}
