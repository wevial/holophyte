import { phaseLabel } from "./runs";

/** What a timeline segment is coloured as. `verify` is reserved: the data
 *  carries no verify boundary yet, so it only appears as the running
 *  segment of a run whose phase folds to verifying. */
export type SegmentKind = "implement" | "review" | "fix" | "verify";

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

/** The slice of `/runs/N` the timeline reads. */
export interface TimelineRun {
  started_ms: number;
  ended_ms?: number | null;
  time_box_ms: number;
  phase: string;
  rounds: { started_ms: number; ended_ms: number | null }[];
}

const LABELS: Record<SegmentKind, string> = {
  implement: "implement",
  review: "review",
  fix: "fix",
  verify: "verify",
};

/** The running segment's kind: an open round is under review; otherwise
 *  the phase decides between verify, the first implement and a fix. */
function runningKind(run: TimelineRun, openRound: boolean): SegmentKind {
  if (openRound) return "review";
  if (phaseLabel(run.phase) === "verifying") return "verify";
  return run.rounds.length === 0 ? "implement" : "fix";
}

/**
 * The run's phases in order, sized against its time box: implement from
 * start to the first round, then each round's review and the fix after
 * it, then a trailing segment from the last boundary to `now` (or to the
 * run's end) labelled with the current phase. Widths are each duration
 * over `time_box_ms`; a run past its box scales them so the sum is 1.
 */
export function segments(run: TimelineRun, now: number): Segment[] {
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

  const total = Math.max(end, cursor) - run.started_ms;
  const scale = run.time_box_ms > 0 ? Math.max(run.time_box_ms, total) : total;
  for (const segment of out) segment.width = scale > 0 ? (segment.to - segment.from) / scale : 0;
  return out;
}

/** Positive: milliseconds left in the box; negative: how far past it. */
export function boxRemaining(run: { started_ms: number; time_box_ms: number }, now: number): number {
  return run.time_box_ms - (now - run.started_ms);
}
