import type { LedgerEntry } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";

const DAY_LOG_LINES = 40;

type LineTone = "live" | "bad" | "act" | "ink-3";

const MARK: Record<LineTone, string> = {
  live: "bg-live",
  bad: "bg-bad",
  act: "bg-act",
  "ink-3": "bg-ink-3",
};

function toneOf(entry: LedgerEntry): LineTone {
  if (entry.kind === "merge") return "live";
  if (entry.kind === "failure") return "bad";
  if (entry.kind === "intervention" && entry.source === "operator") return "act";
  return "ink-3";
}

/** The day's ledger across the scoped hosts, `entries` newest first. */
export function DayLog({ entries }: { entries: LedgerEntry[] }) {
  const shown = entries.slice(0, DAY_LOG_LINES);
  const earlier = entries.length - shown.length;
  return (
    <aside aria-labelledby="day-log" className="day-log min-w-0">
      <h2 id="day-log" className="mt-2.5 mb-2.5 font-sans text-[11px] font-semibold uppercase leading-none tracking-[0.06em] text-ink-3">
        Today's log
      </h2>
      {shown.length === 0 ? (
        <p className="font-mono text-[12.5px] text-ink-3">Nothing logged since midnight.</p>
      ) : (
        <ol className="ml-1 border-l border-rule pt-1 font-mono text-[12.5px] leading-[1.45] text-ink-2">
          {shown.map((entry, index) => (
            <li key={`${entry.at}:${index}`} className="relative grid grid-cols-[44px_minmax(0,1fr)] gap-2.5 py-[7px] pl-4">
              <span data-tone={toneOf(entry)} aria-hidden="true" className={`absolute top-3 -left-1 size-[7px] ${MARK[toneOf(entry)]}`} />
              <time dateTime={new Date(entry.at).toISOString()} className="text-ink-3">
                {formatClock(entry.at)}
              </time>
              <span className="break-words">{entry.ticket == null ? entry.text : `${entry.ticket} · ${entry.text}`}</span>
            </li>
          ))}
          {earlier > 0 && <li className="py-[7px] pl-4 text-ink-3">and {earlier} earlier today</li>}
        </ol>
      )}
    </aside>
  );
}
