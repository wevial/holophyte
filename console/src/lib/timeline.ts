import { phaseLabel, roundLabel } from "./runs";
import type { RunEvent } from "./types";

/** The kind of work or waiting represented by a segment. */
export type SegmentKind = "implement" | "review" | "fix" | "verify" | "merge" | "wait" | "parked";

export interface Segment {
  kind: SegmentKind;
  label: string;
  /** The boundary event’s explanation, when available. */
  reason?: string;
  from: number;
  to: number;
  /** The review or fix round the segment belongs to, when the timeline
   *  numbers it; the tooltip's long name carries it. */
  round?: number;
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
  time_box_ms: number | null;
  phase: string;
  pr_url?: string | null;
  rounds: { round?: number; started_ms: number; ended_ms: number | null }[];
  events?: RunEvent[];
}

const LABELS: Record<SegmentKind, string> = {
  implement: "implement",
  review: "review",
  fix: "fix",
  verify: "verify",
  merge: "merge",
  wait: "wait",
  parked: "parked",
};

/** A segment kind's long name for the tooltip. */
const NAMES: Record<SegmentKind, string> = {
  implement: "Implementation",
  review: "Review",
  fix: "Rework",
  verify: "Verify",
  merge: "Merge",
  wait: "Wait",
  parked: "Parked",
};

/** The tooltip's name for a segment: the kind's long name, plus the round
 *  a numbered review or fix belongs to ("Review · Round 2"). */
export function segmentName(segment: Segment): string {
  if (segment.label === "verifying") return segment.label;
  return segment.round == null ? NAMES[segment.kind] : `${NAMES[segment.kind]} · ${roundLabel(segment.round)}`;
}

/** Store phase → segment kind. Phases missing here (`claimed`, `done`,
 *  `failed`, …) close the open segment and draw none of their own. */
const PHASE_KINDS: Record<string, SegmentKind> = {
  working: "implement",
  verifying: "verify",
  merge_gate: "verify",
  reviewing: "review",
  addressing: "fix",
  merging: "merge",
  awaiting_merge_approval: "parked",
  blocked_on_operator: "parked",
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
  const scale = run.time_box_ms != null && run.time_box_ms > 0 ? Math.max(run.time_box_ms, total) : total;
  for (const segment of out) segment.width = scale > 0 ? (segment.to - segment.from) / scale : 0;
  return out;
}

/**
 * Segments from the run's `phase_change` events in order: each row's `at`
 * closes the previous segment and opens one for the phase after the
 * arrow. Review and fix segments carry recorded round ordinals. The
 * last segment of a live run ends at `now` and pulses.
 */
function fromEvents(run: TimelineRun, changes: RunEvent[], now: number): Segment[] {
  const end = run.ended_ms ?? now;
  const live = run.ended_ms == null;
  const out: Segment[] = [];
  let open: { kind: SegmentKind; label: string; from: number; round?: number; reason?: string } | null = null;
  const rounds = [...run.rounds].sort((a, b) => a.started_ms - b.started_ms);
  /** A segment that picks up where an identical one ended merges into it
   *  (a `working -> working` setup event is one implement phase, not
   *  two): the `to` extends and `running` follows the newer segment. */
  const push = (segment: Segment) => {
    const last = out[out.length - 1];
    if (last && last.to === segment.from && last.kind === segment.kind && last.label === segment.label && last.round === segment.round) {
      last.to = segment.to;
      last.running = segment.running;
    } else out.push(segment);
  };
  const close = (at: number) => {
    if (open && at > open.from) push({ ...open, to: at, running: false, width: 0 });
    open = null;
  };
  let phase: string | null = null;
  let prOpen = false;
  for (const change of changes) {
    if (change.kind === "pull_request") {
      prOpen = true;
      if (phase !== "merge_gate") continue;
    } else phase = phaseAfterArrow(change.summary);
    close(change.at);
    if (phase === "merge_gate" && change.summary.includes(": babysitting")) prOpen = true;
    const monitoring = phase === "merge_gate" && prOpen && !change.summary.includes("pre-merge verify");
    const kind = monitoring ? "wait" : phase == null ? undefined : PHASE_KINDS[phase];
    if (!kind) continue;
    const label = phase === "merge_gate" ? phaseLabel(phase, monitoring ? "open" : null) : LABELS[kind];
    let round: number | undefined;
    if (kind === "review" || kind === "fix") {
      const named = roundNumber(change.summary);
      const recorded = named == null ? -1 : rounds.findIndex((entry) => entry.round === named);
      // A named review may start before its row is recorded. Only unnamed
      // events can fall back to the rounds present at that time.
      const ordinal = named == null ? rounds.filter((entry) => entry.started_ms <= change.at).length : recorded + 1;
      round = ordinal || undefined;
    }
    open = { kind, label, from: change.at, round, reason: change.summary };
  }
  if (open) {
    // Older event streams can lack the PR-open boundary. Only infer the
    // live tail from the current URL; never recolour an explicit verify.
    if (live && phase === "merge_gate" && run.pr_url && !open.reason?.includes("pre-merge verify")) {
      open.kind = "wait";
      open.label = phaseLabel(phase, run.pr_url);
    }
    const last: Segment = { ...open, to: Math.max(open.from, end), running: live, width: 0 };
    if (live || last.to > last.from) push(last);
  }
  const reach = Math.max(end, out.length ? out[out.length - 1]!.to : end);
  return size(out, run, reach);
}

/** The running segment's kind: an open round is under review; otherwise
 *  the phase decides between verify, the first implement and a fix. */
function runningKind(run: TimelineRun, openRound: boolean): SegmentKind {
  if (PHASE_KINDS[run.phase] === "parked") return "parked";
  // Without phase events, a PR URL cannot distinguish monitoring from
  // resumed pre-merge verification. Preserve verification in the fallback.
  if (run.phase === "merge_gate") return "verify";
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
  const push = (kind: SegmentKind, from: number, to: number, round?: number) => {
    if (to > from) out.push({ kind, label: LABELS[kind], from, to, round, running: false, width: 0 });
  };

  let cursor = run.started_ms;
  let openRound = false;
  let openIndex = -1;
  rounds.forEach((round, index) => {
    push(index === 0 ? "implement" : "fix", cursor, round.started_ms, index === 0 ? undefined : index);
    if (round.ended_ms == null) {
      openRound = true;
      openIndex = index;
      cursor = round.started_ms;
      return;
    }
    push("review", round.started_ms, round.ended_ms, index + 1);
    cursor = round.ended_ms;
  });

  const kind = runningKind(run, openRound);
  const label = running ? phaseLabel(run.phase) : LABELS[kind];
  const round = kind === "review" ? openIndex + 1 : kind === "fix" && rounds.length > 0 ? rounds.length : undefined;
  out.push({ kind, label, from: cursor, to: Math.max(cursor, end), round, running, width: 0 });
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
    .filter((event) => event.kind === "phase_change" ||
      (event.kind === "pull_request" && (event.summary.startsWith("pull request open") ||
        event.summary.startsWith("adopted the branch's open pull request"))))
    .sort((a, b) => a.at - b.at);
  return changes.some((event) => event.kind === "phase_change") ? fromEvents(run, changes, now) : fromRounds(run, now);
}

/** Positive: milliseconds left in the box; negative: how far past it. */
export function boxRemaining(run: { started_ms: number; time_box_ms: number | null }, now: number): number | null {
  return run.time_box_ms == null ? null : run.time_box_ms - (now - run.started_ms);
}
