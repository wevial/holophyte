import { formatAge, formatClock } from "./format";
import { roundNumber } from "./timeline";
import type { Round, RunEvent } from "./types";

/** The events oldest first, so the newest row is last however they arrived. */
export function orderEvents(events: RunEvent[]): RunEvent[] {
  return [...events].sort((a, b) => a.at - b.at);
}

/** `FROM -> TO: detail` → `[FROM, TO]`; null when the summary is not that shape. */
function edge(summary: string): [string, string] | null {
  const match = /^\s*([a-z_]+)\s*->\s*([a-z_]+)/.exec(summary);
  return match ? [match[1]!, match[2]!] : null;
}

/** The review round a phase change belongs to: the one its detail names
 *  (`round 3 review`), else the newest of `rounds[]` open by then. */
function roundOf(event: RunEvent, rounds: Round[]): number | null {
  const named = roundNumber(event.summary);
  if (named != null) return named;
  const begun = rounds.filter((round) => round.started_ms <= event.at);
  return begun.length > 0 ? Math.max(...begun.map((round) => round.round)) : null;
}

const plural = (count: number, noun: string) => `${count} ${noun}${count === 1 ? "" : "s"}`;

/**
 * One short line per event, the handoff's words for the loop's phase
 * changes: `claimed -> working: cutting …` reads `run started ·
 * implementing`, `verifying -> reviewing: round 2 review` reads `review
 * round 2 started`, and `reviewing -> addressing` counts the round's
 * findings from `rounds[]`. A transition without a phrase here reads
 * `FROM → TO`; any other kind keeps its own summary.
 */
export function summarize(event: RunEvent, rounds: Round[] = []): string {
  if (event.kind !== "phase_change") return event.summary || event.kind;
  const walked = edge(event.summary);
  if (!walked) return event.summary || event.kind;
  const [from, to] = walked;
  const round = () => roundOf(event, rounds);
  switch (`${from} -> ${to}`) {
    case "claimed -> working":
      return "run started · implementing";
    case "working -> verifying":
      return "implement done · verifying";
    case "verifying -> reviewing": {
      const n = round();
      return n == null ? "review started" : `review round ${n} started`;
    }
    case "reviewing -> addressing": {
      const n = round();
      const found = n == null ? undefined : rounds.find((r) => r.round === n)?.findings.length;
      const head = n == null ? "review" : `review round ${n}`;
      return found == null ? `${head} · changes requested` : `${head} · ${plural(found, "finding")}`;
    }
    case "addressing -> verifying":
      return "fix applied · verifying";
    case "reviewing -> merge_gate":
      return "review approved · merge gate";
    case "merge_gate -> merging":
      return "merging";
    case "merging -> done":
      return "merged";
    default:
      return `${from} → ${to}`;
  }
}

/** The header's summary: `6 events · last: heartbeat 4s ago`; `1 event`
 *  in the singular; `No events yet` before the first. The last event
 *  reads as its row does (`summarize`). */
export function logSummary(events: RunEvent[], now: number, rounds: Round[] = []): string {
  if (events.length === 0) return "No events yet";
  const ordered = orderEvents(events);
  const last = ordered[ordered.length - 1]!;
  const count = `${events.length} ${events.length === 1 ? "event" : "events"}`;
  return `${count} · last: ${summarize(last, rounds)} ${formatAge(now - last.at)} ago`;
}

/** The header's right side: `14:07 → 14:31`, first event to last; empty
 *  without events. */
export function timeRange(events: RunEvent[]): string {
  if (events.length === 0) return "";
  const ordered = orderEvents(events);
  return `${formatClock(ordered[0]!.at)} → ${formatClock(ordered[ordered.length - 1]!.at)}`;
}
