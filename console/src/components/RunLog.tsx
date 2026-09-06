import { useState } from "react";
import { formatClock } from "../lib/format";
import { logSummary, orderEvents, timeRange } from "../lib/log";
import type { RunEvent } from "../lib/types";

/** The card's footer: the run's narrative events from `/runs/N`, newest
 *  last, under a header that folds them away. Open by default; the fold
 *  lives with the mounted card, so a re-expanded row opens again. */
export function RunLog({ events, now }: { events: RunEvent[]; now: number }) {
  const [open, setOpen] = useState(true);
  const rows = orderEvents(events);
  return (
    <section data-run-log className="-mx-4 -mb-3 mt-3 rounded-b-[10px] bg-rail px-4 py-2 font-mono text-[12px]">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((previous) => !previous)}
        className="flex w-full items-baseline gap-3 text-left"
      >
        <span data-chevron={open ? "open" : "closed"} aria-hidden="true" className="text-rail-sub">
          {open ? "▾" : "▸"}
        </span>
        <span className="text-[12px] font-semibold uppercase tracking-wide text-rail-sub">Run log</span>
        <span data-log-summary className="truncate text-rail-text">
          {logSummary(events, now)}
        </span>
        <span data-log-range className="ml-auto whitespace-nowrap text-rail-faint">
          {timeRange(events)}
        </span>
      </button>
      {open && rows.length > 0 && (
        <ol data-log-rows className="mt-2 flex flex-col gap-[2px]">
          {rows.map((event, index) => (
            <li key={`${event.at}:${index}`} data-log-row className="grid grid-cols-[52px_1fr] gap-x-2">
              <span className="text-rail-faint">{formatClock(event.at)}</span>
              <span className="min-w-0 break-words text-rail-text">{event.summary || event.kind}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
