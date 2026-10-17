import type { History, HistoryRow } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import type { HostRecord } from "../lib/hosts";
import { localMidnight } from "../lib/ledger";
import type { ContractBodies } from "./fleet";
import { stateGroup } from "./history";
import { stateOf, type State } from "./state";

export const PAGE_DAYS = 5;
export const PAGE_COUNT = 6;

export interface DayWindow {
  since: number;
  until: number;
}

export type DayAnswers = Map<number, Map<string, History>>;

export interface DayRun extends HistoryRow {
  base: string;
  host: string;
  project: string;
  state: State;
}

export interface DayGroup extends DayWindow {
  rows: DayRun[];
  counts: { runs: number; merged: number; failed: number; abandoned: number };
  cut: { limit: number; total: number } | null;
  workMs: number | null;
  estimateMs: number | null;
}

export function pastDays(now: number): DayWindow[] {
  let until = localMidnight(localMidnight(now) - 1);
  return Array.from({ length: PAGE_DAYS * PAGE_COUNT }, () => {
    const since = localMidnight(until - 1);
    const day = { since, until };
    until = since;
    return day;
  });
}

export function dayGroups(hosts: HostRecord<ContractBodies>[], answers: DayAnswers, days: DayWindow[]): DayGroup[] {
  return days.map((day) => {
    const capped = { limit: 0, total: 0 };
    for (const host of hosts) {
      const answer = answers.get(day.since)?.get(host.key);
      if (answer?.truncated) {
        capped.limit += answer.limit;
        capped.total += answer.total;
      }
    }
    const rows = hosts.flatMap((host): DayRun[] => {
      const status = host.status;
      const answer = answers.get(day.since)?.get(host.key);
      if (!status || !answer) return [];
      return answer.rows.map((run) => ({
        ...run,
        base: host.base,
        host: host.key,
        project: projectName(status.project),
        state: stateOf({ run, staleMs: status.thresholds.heartbeat_stale_ms }),
      }));
    }).sort((a, b) => b.started_ms - a.started_ms || b.id - a.id);
    const counts = { runs: rows.length, merged: 0, failed: 0, abandoned: 0 };
    let workMs: number | null = null;
    let estimateMs: number | null = null;
    for (const row of rows) {
      const group = stateGroup(row.state);
      if (group !== "active") counts[group] += 1;
      if (row.working_ms != null && row.estimate_ms != null) {
        workMs = (workMs ?? 0) + row.working_ms;
        estimateMs = (estimateMs ?? 0) + row.estimate_ms;
      }
    }
    return { ...day, rows, counts, cut: capped.total > 0 ? capped : null, workMs, estimateMs };
  });
}
