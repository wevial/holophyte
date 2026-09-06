import { phaseLabel } from "./runs";
import type { RunEvent } from "./types";

/** What a timeline segment is coloured as: implement and fix teal, review
 *  purple, verify and merge green. */
export type SegmentKind = "implement" | "review" | "fix" | "verify" | "merge";

export interface Segment {
  kind: SegmentKind;
  label: string;
  from: number;
  to: number;
  /** The trailing segment of a live run: it ends at `now` and keeps growing. */
  running: boolean;
  /** Share of the bar, 0..1: the duration over the time box, or over the
   *  whole run once that is longer than the box. */
  width: number;
}

/** The slice of `/runs/N` the timeline reads. `events` carries the run's
 *  narrative stream; its `phase_change` rows are the segment boundaries,
 *  and a run without any falls back to its finished `rounds`. */
export interface TimelineRun {
  started_ms: number;
  ended_ms?: number | null;
  time_box_ms: number;
  phase: string;
  rounds: { started_ms: number; ended_ms: number | null }[];
  events?: RunEvent[];
}

const LABELS: Record<SegmentKind, string> = {
  implement: "implement",
  review: "review",
  fix: "fix",
  verify: "verify",
  merge: "merge",
};

/** Store phase → segment kind. Phases missing here (`claimed`, `done`,
 *  `failed`, …) close the open segment and draw none of their own. */
const PHASE_KINDS: Record<string, SegmentKind> = {
  working: "implement",
  verifying: "verify",
  merge_gate: "verify",
  reviewing: "review",
  addressing: "fix",
  merging: "merge",
};

/** `FROM -> TO: detail` → `TO`; null when the summary is not that shape. */
export function phaseAfterArrow(summary: string): string | null {
  const match = /->\s*([a-z_]+)/.exec(summary);
  return match ? match[1]! : null;
}

/** The round a phase change names (`round 3 review`, `round 3: …`), or null. */
export function roundNumber(summary: string): number | null {
  const match = /\bround\s+(\d+)/i.exec(summary);
  return match ? Number(match[1]) : null;
}

/** Widths as each duration over `time_box_ms`; a run past its box scales
 *  them so the sum is 1. */
function size(out: Segment[], run: TimelineRun, end: number): Segment[] {
  const total = end - run.started_ms;
  const scale = run.time_box_ms > 0 ? Math.max(run.time_box_ms, total) : total;
  for (const segment of out) segment.width = scale > 0 ? (segment.to - segment.from) / scale : 0;
  return out;
}

/**
 * Segments from the run's `phase_change` events in order: each row's `at`
 * closes the previous segment and opens one for the phase after the
 * arrow. Review and fix segments carry the round the summary names. The
 * last segment of a live run ends at `now` and pulses.
 */
function fromEvents(run: TimelineRun, changes: RunEvent[], now: number): Segment[] {
  const end = run.ended_ms ?? now;
  const live = run.ended_ms == null;
  const out: Segment[] = [];
  let open: { kind: SegmentKind; label: string; from: number } | null = null;
  let reviews = 0;
  const close = (at: number) => {
    if (open && at > open.from) out.push({ ...open, to: at, running: false, width: 0 });
    open = null;
  };
  for (const change of changes) {
    close(change.at);
    const phase = phaseAfterArrow(change.summary);
    const kind = phase == null ? undefined : PHASE_KINDS[phase];
    if (!kind) continue;
    let label = LABELS[kind];
    if (kind === "review") reviews = roundNumber(change.summary) ?? reviews + 1;
    if (kind === "review" || kind === "fix") label = `${label} ${roundNumber(change.summary) ?? reviews}`;
    open = { kind, label, from: change.at };
  }
  if (open) {
    const last: Segment = { ...open, to: Math.max(open.from, end), running: live, width: 0 };
    if (live || last.to > last.from) out.push(last);
  }
  const reach = Math.max(end, out.length ? out[out.length - 1]!.to : end);
  return size(out, run, reach);
}

/** The running segment's kind: an open round is under review; otherwise
 *  the phase decides between verify, the first implement and a fix. */
function runningKind(run: TimelineRun, openRound: boolean): SegmentKind {
  if (openRound) return "review";
  if (phaseLabel(run.phase) === "verifying") return "verify";
  return run.rounds.length === 0 ? "implement" : "fix";
}

/** The fallback for a run without `phase_change` events: implement from
 *  start to the first round, then each round's review and the fix after
 *  it, then a trailing segment from the last boundary to `now` (or to the
 *  run's end) labelled with the current phase. */
function fromRounds(run: TimelineRun, now: number): Segment[] {
  const end = run.ended_ms ?? now;
  const running = run.ended_ms == null;
  const rounds = [...run.rounds].sort((a, b) => a.started_ms - b.started_ms);
  const out: Segment[] = [];
  const push = (kind: SegmentKind, from: number, to: number, label = LABELS[kind]) => {
    if (to > from) out.push({ kind, label, from, to, running: false, width: 0 });
  };

  let cursor = run.started_ms;
  let openRound = false;
  rounds.forEach((round, index) => {
    push(index === 0 ? "implement" : "fix", cursor, round.started_ms);
    if (round.ended_ms == null) {
      openRound = true;
      cursor = round.started_ms;
      return;
    }
    push("review", round.started_ms, round.ended_ms);
    cursor = round.ended_ms;
  });

  const kind = runningKind(run, openRound);
  const label = running ? phaseLabel(run.phase) : LABELS[kind];
  out.push({ kind, label, from: cursor, to: Math.max(cursor, end), running, width: 0 });
  return size(out, run, Math.max(end, cursor));
}

/**
 * The run's phases in order, sized against its time box. With
 * `phase_change` events the segments follow them as they happen; without
 * any, the finished `rounds` split the bar as before. Widths are each
 * duration over `time_box_ms`; a run past its box scales them so the sum
 * is 1.
 */
export function buildTimeline(run: TimelineRun, now: number): Segment[] {
  const changes = (run.events ?? [])
    .filter((event) => event.kind === "phase_change")
    .sort((a, b) => a.at - b.at);
  return changes.length > 0 ? fromEvents(run, changes, now) : fromRounds(run, now);
}

/** Positive: milliseconds left in the box; negative: how far past it. */
export function boxRemaining(run: { started_ms: number; time_box_ms: number }, now: number): number {
  return run.time_box_ms - (now - run.started_ms);
}
