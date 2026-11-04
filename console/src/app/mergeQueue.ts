import type { MergeQueue, MergeQueueCheck, MergeQueueConfiguration, MergeQueueEntry, MergeQueueRun } from "../lib/contract.gen";
import { formatClock, formatDuration, formatSpan } from "../lib/format";
import { phaseLabel } from "../lib/runs";
import type { State, Tone } from "./state";

const SECOND = 1000;

const STRATEGY_WORDS: Record<string, string> = { ALLGREEN: "all green", HEADGREEN: "head green" };

export const isBuilding = (entry: MergeQueueEntry): boolean => entry.state !== "QUEUED";

export function queueCounts(queue: MergeQueue): { queued: number; building: number } {
  return { queued: queue.entries.length, building: queue.entries.filter(isBuilding).length };
}

/** When the last entry merges: the answer's `now` plus its estimate; null
 *  with no entries or no estimate. */
export function clearsAt(queue: MergeQueue): number | null {
  const last = queue.entries.at(-1);
  return last?.estimated_s == null ? null : queue.now + last.estimated_s * SECOND;
}

const minutes = (seconds: number): number => Math.max(1, Math.round(seconds / 60));

export const mergesIn = (seconds: number | null): string => (seconds == null ? "–" : `~${minutes(seconds)}m`);

/** The queue's settings in words: `squash · all green · builds 1 at a time · checks time out at 60m`. */
export function configurationWords(configuration: MergeQueueConfiguration | null): string {
  if (configuration == null) return "";
  const { merge_method: method, merging_strategy: strategy, maximum_entries_to_build: builds, check_response_timeout: timeout } = configuration;
  return [
    method?.toLowerCase(),
    strategy == null ? null : (STRATEGY_WORDS[strategy] ?? strategy.toLowerCase()),
    builds == null ? null : `builds ${builds} at a time`,
    timeout == null ? null : `checks time out at ${timeout}m`,
  ].filter((part) => part != null).join(" · ");
}

/** The run's phase in words, with its review round when it has had one. */
export function runWords(run: MergeQueueRun, round: boolean): string {
  const phase = phaseLabel(run.phase);
  return round && run.round > 0 ? `${phase} · round ${run.round}` : phase;
}

export interface CheckSplit {
  checks: MergeQueueCheck[];
  done: number;
}

const split = (checks: MergeQueueCheck[]): CheckSplit => ({ checks, done: checks.filter((check) => check.status === "completed").length });

export function splitChecks(checks: MergeQueueCheck[]): { required: CheckSplit; other: CheckSplit } {
  return { required: split(checks.filter((check) => check.required)), other: split(checks.filter((check) => !check.required)) };
}

const GOOD = new Set(["success", "neutral"]);

/** A check's icon, tone and words at `now`. */
export function checkLook(check: MergeQueueCheck, now: number): { state: State; tone: Tone; words: string } {
  if (check.status === "in_progress") {
    return { state: "running", tone: "live", words: `running ${formatSpan(now - (check.started_ms ?? now))}` };
  }
  if (check.status !== "completed") return { state: "queued", tone: "ink-3", words: "waits" };
  if (check.conclusion === "skipped") return { state: "abandoned", tone: "ink-3", words: "skipped" };
  const took = check.started_ms == null || check.completed_ms == null ? "" : formatDuration(check.completed_ms - check.started_ms);
  return GOOD.has(check.conclusion ?? "") ? { state: "ready_to_merge", tone: "live", words: took } : { state: "failed", tone: "bad", words: took || (check.conclusion ?? "") };
}

/** The open row's line: its merge group, building since, added and merges at. */
export function entryLine(entry: MergeQueueEntry, now: number): string {
  return [
    entry.group_sha == null ? null : `merge group ${entry.group_sha.slice(0, 7)}`,
    entry.building_ms == null ? null : `building since ${formatClock(entry.building_ms)}`,
    entry.enqueued_ms == null ? null : `added ${formatClock(entry.enqueued_ms)}${entry.pr.author ? ` by ${entry.pr.author}` : ""}`,
    entry.estimated_s == null ? null : `merges ~${formatClock(now + entry.estimated_s * SECOND)}`,
  ].filter((part) => part != null).join(" · ");
}

interface JumpCost {
  restarts: number[];
  estimate: number | null;
  drops: { number: number; minutes: number } | null;
}

export function jumpCost(queue: MergeQueue, entry: MergeQueueEntry): JumpCost {
  const first = queue.entries.find((each) => each.position === 1);
  return {
    restarts: queue.entries.filter((each) => each.position < entry.position).map((each) => each.pr.number),
    estimate: first?.estimated_s == null ? null : minutes(first.estimated_s),
    drops: first != null && isBuilding(first) && first.building_ms != null
      ? { number: first.pr.number, minutes: minutes((queue.now - first.building_ms) / SECOND) }
      : null,
  };
}
