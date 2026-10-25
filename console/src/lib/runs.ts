import { projectName } from "./derive";
import type { Queued, Run, RunEvent, Status } from "./types";

const BOOKKEEPING_KINDS = new Set(["pr_seen_commits", "pr_empty_wakes", "pr_wake_breaker", "pr_text_sha"]);

/** The newest `count` actions, newest first regardless of event order;
 *  supervisor bookkeeping is not activity. */
export function latestEvents(events: RunEvent[], count: number): RunEvent[] {
  return events
    .filter((event) => !BOOKKEEPING_KINDS.has(event.kind))
    .sort((a, b) => b.at - a.at)
    .slice(0, count);
}

const PHASE_LABELS: Record<string, string> = {
  working: "implementing",
  addressing: "implementing",
  verifying: "verifying",
  merge_gate: "verifying",
  reviewing: "reviewing",
};

/** `working`/`addressing` → implementing, `verifying`/`merge_gate` →
 *  verifying (or monitoring PR once a PR exists), `reviewing` → reviewing;
 *  any other phase is its own label. */
export function phaseLabel(phase: string, pr_url?: string | null, note?: string | null): string {
  if (phase === "merge_gate" && note?.startsWith("waiting for merge lock (run ")) return "waiting for merge lock";
  if (phase === "merge_gate" && pr_url) return "monitoring PR";
  return PHASE_LABELS[phase] ?? phase;
}

/** Elapsed as a share of the time box in percent, capped at 100 so a run
 *  past its box fills the bar and no further. A box of nothing is full
 *  the moment any time has elapsed. */
export function boxPercent(elapsedMs: number, boxMs: number): number {
  if (boxMs <= 0) return elapsedMs > 0 ? 100 : 0;
  return Math.min(100, Math.max(0, (elapsedMs / boxMs) * 100));
}

/** One `/status` body and the base URL of the daemon that answered it.
 *  `seen_ms` is the console's clock when the body last arrived, the
 *  reference a drawing site ages the body's frozen numbers from. */
export interface DaemonStatus {
  base: string;
  status: Status;
  seen_ms?: number | null;
}

/** One project's block of runs: the daemon that serves it, its
 *  live runs and its tickets queued for the next claim. Two daemons serving one project path are two blocks, each
 *  under its own daemon's supervisor line. */
export interface ProjectGroup {
  /** The project's path, as the rail selects it. */
  path: string;
  name: string;
  /** The daemon's base URL: where the block's run details are read. */
  base: string;
  status: Status;
  /** The console's clock when `status` last arrived; ages drawn from it
   *  keep counting against the local clock between polls. */
  seen_ms?: number | null;
  runs: Run[];
  queued?: Queued[];
}

/** One group per (project, daemon) across the given bodies, in the order
 *  first seen; two bodies from one daemon for one path pool their runs
 *  under the first. */
export function groupByProject(daemons: DaemonStatus[]): ProjectGroup[] {
  const groups: ProjectGroup[] = [];
  for (const { base, status, seen_ms } of daemons) {
    const path = status.project;
    const existing = groups.find((group) => group.path === path && group.base === base);
    if (existing) {
      existing.runs.push(...status.runs);
      existing.queued?.push(...(status.queued ?? []));
    } else {
      groups.push({ path, name: projectName(path), base, status, seen_ms, runs: [...status.runs], queued: [...(status.queued ?? [])] });
    }
  }
  return groups;
}

/** A run's selection key: the same id on two daemons is two
 *  rows, so the key names the daemon the run belongs to. */
export function runKey(base: string, id: number): string {
  return `${base}#${id}`;
}

/** The API already includes active work through its snapshot time. */
export function workingMs(run: { working_ms?: number | null; work_started_ms?: number | null }, sinceMs = 0): number | null {
  return run.working_ms == null ? null : run.working_ms + (run.work_started_ms != null ? sinceMs : 0);
}

/** The clock the time box judges: agent turns without verify, which counts
 *  on between polls only while a turn (not a verify) is open. A daemon too
 *  old to serve `agent_ms` falls back to `workingMs()`. */
export function agentMs(
  run: { agent_ms?: number | null; working_ms?: number | null; work_started_ms?: number | null; verify_started_ms?: number | null },
  sinceMs = 0,
): number | null {
  if (run.agent_ms == null) return workingMs(run, sinceMs);
  return run.agent_ms + (run.work_started_ms != null && run.verify_started_ms == null ? sinceMs : 0);
}
