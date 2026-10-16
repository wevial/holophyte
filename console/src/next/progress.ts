import type { ConsoleAsk, RunDetail } from "../lib/contract.gen";
import { findingPath, severityOf } from "../lib/findings";
import { formatClock } from "../lib/format";
import { summarize } from "../lib/log";
import { shortSha } from "../lib/shipped";
import { failureWord } from "./history";

export type ProgressKind = "event" | "loop" | "round" | "steer" | "ask" | "merge" | "failure";

export interface ProgressLine {
  at: number;
  kind: ProgressKind;
  text: string;
  sub?: string[];
}

/** Ties at one millisecond: an event, then the round that ended at it, the loop back it sent, a steer or ask, then the run's end. */
const KIND_ORDER: Record<ProgressKind, number> = { event: 0, round: 1, loop: 2, steer: 3, ask: 3, merge: 4, failure: 4 };

const PASSED = new Set(["pass", "approved"]);

const LOOP_BACK = /^\s*reviewing\s*->\s*addressing\b/;

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

function firstLine(text: string | null | undefined): string {
  return (text ?? "").split("\n", 1)[0]!.trim();
}

function eventLines(detail: RunDetail): ProgressLine[] {
  return detail.events.map((event) =>
    event.kind === "phase_change" && LOOP_BACK.test(event.summary)
      ? { at: event.at, kind: "loop", text: "Back to implement" }
      : { at: event.at, kind: "event", text: capital(summarize(event, detail.rounds)) },
  );
}

function roundLines(detail: RunDetail): ProgressLine[] {
  return detail.rounds.flatMap((round) => {
    if (round.ended_ms == null) return [];
    const must = round.findings.filter((finding) => severityOf(finding) === "must").length;
    const outcome =
      round.verdict === "changes_requested"
        ? `changes requested, ${must} must-fix`
        : PASSED.has(round.verdict)
          ? "passed"
          : round.verdict.replaceAll("_", " ");
    const sub = round.findings.map((finding) => {
      const path = finding.path ? findingPath(finding) : "";
      const where = path && finding.line != null ? `${path}:${finding.line}` : path;
      return [finding.severity.toUpperCase(), firstLine(finding.message), where].filter(Boolean).join(" · ");
    });
    return [{ at: round.ended_ms, kind: "round", text: `Review round ${round.round}: ${outcome}`, sub }];
  });
}

/** A ticket's note made after this run ended is for the run that follows, unless this run read it. */
function steerLines(detail: RunDetail): ProgressLine[] {
  const { id, outcome, ended_ms: ended } = detail.run;
  const after = (at: number) => outcome != null && ended != null && at > ended;
  return detail.steer_notes
    .filter((note) => !note.withdrawn || note.consumed_ms != null)
    .filter((note) => note.consumed_by === id || !after(note.at))
    .map((note) => ({
      at: note.at,
      kind: "steer",
      text: `${note.kind === "hint" ? "You hinted" : "You steered"}: ${note.note}`,
      sub: [note.consumed_ms == null ? "→ waiting until the agent reads it" : `→ the agent read it at ${formatClock(note.consumed_ms)}`],
    }));
}

function askLines(asks: ConsoleAsk[]): ProgressLine[] {
  return asks.map((ask) => ({
    at: ask.asked_ms,
    kind: "ask",
    text: `You asked: ${ask.question}`,
    ...(ask.answered_ms == null ? {} : { sub: [`→ answered: ${firstLine(ask.answer)}`] }),
  }));
}

function endLine(detail: RunDetail, now: number): ProgressLine[] {
  const { run } = detail;
  if (run.outcome == null) return [];
  const at = run.ended_ms ?? now;
  if (run.outcome === "merged") {
    const sha = shortSha(run.merge_sha);
    return [{ at, kind: "merge", text: sha ? `Merged to main as ${sha}` : "Merged to main" }];
  }
  const word = capital(run.outcome === "failed" ? failureWord(run.failure_kind) : run.outcome);
  const reason = firstLine(run.outcome_reason);
  return [{ at, kind: "failure", text: reason ? `${word}: ${reason}` : word }];
}

/** The run's story oldest first: its events, ended review rounds with their
 *  findings, steer notes, asks, and its end once it has one. */
export function progressLines(detail: RunDetail, asks: ConsoleAsk[], now: number): ProgressLine[] {
  const lines = [...eventLines(detail), ...roundLines(detail), ...steerLines(detail), ...askLines(asks), ...endLine(detail, now)];
  return lines.sort((a, b) => a.at - b.at || KIND_ORDER[a.kind] - KIND_ORDER[b.kind]);
}
