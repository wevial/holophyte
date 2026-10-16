import { useState } from "react";
import type { LedgerEntry } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";

const DAY_LOG_LINES = 25;

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

/** The entry's first line, cut at a code fence. */
function summaryOf(text: string): string {
  return text.trim().split("```")[0]!.split("\n")[0]!.trim();
}

function DayLogEntry({ entry }: { entry: LedgerEntry }) {
  const [open, setOpen] = useState(false);
  const summary = summaryOf(entry.text);
  const more = summary !== entry.text.trim();
  const line = entry.ticket == null ? summary : `${entry.ticket} · ${summary}`;
  return (
    <li className="relative grid grid-cols-[44px_minmax(0,1fr)] gap-2.5 py-[7px] pl-4">
      <span data-tone={toneOf(entry)} aria-hidden="true" className={`absolute top-3 -left-1 size-[7px] ${MARK[toneOf(entry)]}`} />
      <time dateTime={new Date(entry.at).toISOString()} className="text-ink-3">
        {formatClock(entry.at)}
      </time>
      <div className="min-w-0">
        {more ? (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
            className="line-clamp-2 w-full cursor-pointer break-words text-left hover:text-ink"
          >
            {line}
          </button>
        ) : (
          <span className="line-clamp-2 break-words">{line}</span>
        )}
        {open && (
          <pre className="mt-1.5 max-h-[calc(16*1.45em)] overflow-y-auto whitespace-pre-wrap break-words border-l border-rule pl-2 text-ink-3">
            {entry.text}
          </pre>
        )}
      </div>
    </li>
  );
}

/** The day's ledger across the scoped hosts, `entries` newest first. */
export function DayLog({ entries }: { entries: LedgerEntry[] }) {
  const shown = entries.slice(0, DAY_LOG_LINES);
  const earlier = entries.length - shown.length;
  return (
    <aside aria-labelledby="day-log" className="day-log min-w-0 max-h-[calc(100vh-40px)] overflow-y-auto">
      <h2 id="day-log" className="mt-2.5 mb-2.5 font-sans text-[11px] font-semibold uppercase leading-none tracking-[0.06em] text-ink-3">
        Today's log
      </h2>
      {shown.length === 0 ? (
        <p className="font-mono text-[12.5px] text-ink-3">Nothing logged since midnight.</p>
      ) : (
        <ol className="ml-1 border-l border-rule pt-1 font-mono text-[12.5px] leading-[1.45] text-ink-2">
          {shown.map((entry, index) => (
            <DayLogEntry key={`${entry.at}:${index}`} entry={entry} />
          ))}
          {earlier > 0 && <li className="py-[7px] pl-4 text-ink-3">and {earlier} earlier today</li>}
        </ol>
      )}
    </aside>
  );
}
