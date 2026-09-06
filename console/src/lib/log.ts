import { formatAge, formatClock } from "./format";
import type { RunEvent } from "./types";

/** The events oldest first, so the newest row is last however they arrived. */
export function orderEvents(events: RunEvent[]): RunEvent[] {
  return [...events].sort((a, b) => a.at - b.at);
}

/** The header's summary: `6 events · last: heartbeat 4s ago`; `1 event`
 *  in the singular; `No events yet` before the first. */
export function logSummary(events: RunEvent[], now: number): string {
  if (events.length === 0) return "No events yet";
  const ordered = orderEvents(events);
  const last = ordered[ordered.length - 1]!;
  const count = `${events.length} ${events.length === 1 ? "event" : "events"}`;
  return `${count} · last: ${last.summary || last.kind} ${formatAge(now - last.at)} ago`;
}

/** The header's right side: `14:07 → 14:31`, first event to last; empty
 *  without events. */
export function timeRange(events: RunEvent[]): string {
  if (events.length === 0) return "";
  const ordered = orderEvents(events);
  return `${formatClock(ordered[0]!.at)} → ${formatClock(ordered[ordered.length - 1]!.at)}`;
}
