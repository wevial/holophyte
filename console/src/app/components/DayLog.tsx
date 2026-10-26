import { useId, useLayoutEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import type { LedgerEntry } from "../../lib/contract.gen";
import { projectName } from "../../lib/derive";
import { formatClock } from "../../lib/format";
import { prLabel } from "../../lib/shipped";
import { categoryOf, keeps, phraseOf, projectColours, useDayLogFilter, type DayLogFilter } from "../dayLog";
import { runPage } from "../run";
import { keepScope, useConsolePeers } from "../scope";
import { SettingsIcon } from "./icons";

const DAY_LOG_LINES = 25;

type LineTone = "live" | "bad" | "act" | "ask" | "ink-3";

const MARK: Record<LineTone, string> = {
  live: "bg-live",
  bad: "bg-bad",
  act: "bg-act",
  ask: "bg-ask",
  "ink-3": "bg-ink-3",
};

function toneOf(entry: LedgerEntry): LineTone {
  const { group, item } = categoryOf(entry);
  if (item === "merged") return "live";
  if (item === "failed") return "bad";
  if (group === "needs-you") return "ask";
  if (group === "operator") return "act";
  return "ink-3";
}

const CHIP = "border border-rule px-1.5 leading-[1.6] text-[12px] text-ink-2 hover:border-ink-3 hover:text-ink";

function Chips({ entry }: { entry: LedgerEntry }) {
  const { pr_url: pr, run, project } = entry;
  if (pr == null && (run == null || project == null)) return null;
  return (
    <span className="mt-1 flex flex-wrap gap-1.5">
      {pr != null && (
        <a href={pr} target="_blank" rel="noopener noreferrer" className={CHIP}>
          {prLabel(pr).replace(/^PR (?=#)/, "")}
        </a>
      )}
      {run != null && project != null && (
        <Link {...runPage(project, run)} className={CHIP}>
          run {run}
        </Link>
      )}
    </span>
  );
}

function TicketKey({ entry, colour }: { entry: LedgerEntry; colour: string | undefined }) {
  if (entry.ticket == null) return null;
  const style = colour ? { color: colour } : undefined;
  if (entry.project == null) return <span className="font-semibold" style={style}>{entry.ticket}</span>;
  return (
    <Link to="/tickets/$project/$key" params={{ project: projectName(entry.project), key: entry.ticket }} search={keepScope}
      className="font-semibold hover:underline" style={style}>
      {entry.ticket}
    </Link>
  );
}

function DayLogEntry({ entry, quiet, colour }: { entry: LedgerEntry; quiet: boolean; colour: string | undefined }) {
  const [open, setOpen] = useState(false);
  const phrase = phraseOf(entry);
  const [clipped, setClipped] = useState(false);
  const [element, setElement] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (!element) return;
    const measure = () => setClipped(element.scrollHeight > element.clientHeight);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [element, phrase]);
  const more = clipped || phrase !== entry.text.trim();
  const tone = quiet ? "ink-3" : toneOf(entry);
  const phraseTone = quiet ? "text-ink-3" : tone === "ask" ? "text-ask-ink" : "";
  return (
    <li data-quiet={quiet || undefined} className={`relative grid grid-cols-[44px_minmax(0,1fr)] gap-2.5 py-[7px] pl-4 ${quiet ? "text-ink-3" : ""}`}>
      <span data-tone={tone} aria-hidden="true" className={`absolute top-3 -left-1 size-[7px] ${MARK[tone]}`} />
      <time dateTime={new Date(entry.at).toISOString()} className="text-ink-3">
        {formatClock(entry.at)}
      </time>
      <div className="min-w-0">
        <span className="flex flex-wrap items-baseline gap-x-2">
          <span className={quiet ? "opacity-75" : ""}>
            <TicketKey entry={entry} colour={colour} />
          </span>
          {more ? (
            <button
              ref={setElement}
              data-phrase
              type="button"
              aria-expanded={open}
              onClick={() => setOpen(!open)}
              className={`line-clamp-2 min-w-0 cursor-pointer break-words text-left hover:text-ink ${phraseTone}`}
            >
              {phrase}
            </button>
          ) : (
            <span ref={setElement} data-phrase className={`line-clamp-2 min-w-0 break-words ${phraseTone}`}>
              {phrase}
            </span>
          )}
        </span>
        <Chips entry={entry} />
        {open && (
          <pre className="mt-1.5 max-h-[calc(16*1.45em)] overflow-y-auto whitespace-pre-wrap break-words border-l border-rule pl-2 text-ink-3">
            {entry.text}
          </pre>
        )}
      </div>
    </li>
  );
}

/** A key per entry from its content, so entries arriving above keep their state. */
function keysOf(entries: LedgerEntry[]): string[] {
  const seen = new Map<string, number>();
  return entries.map((entry) => {
    const base = `${entry.at}:${entry.source}:${entry.kind}:${entry.ticket ?? ""}:${entry.text}`;
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    return `${base}:${count}`;
  });
}

/** The day's ledger across the scoped hosts, `entries` newest first, kept
 *  to the saved filter unless Show all is pressed; a `preview` shows the
 *  entries that filter keeps, with their count. */
export function DayLog({ entries, preview, className = "" }: { entries: LedgerEntry[]; preview?: DayLogFilter; className?: string }) {
  const saved = useDayLogFilter();
  const filter = preview ?? saved.filter;
  const heading = useId();
  const Heading = preview ? "h3" : "h2";
  const { hosts } = useConsolePeers();
  const colours = projectColours(hosts);
  const [showAll, setShowAll] = useState(false);
  const kept = entries.filter((entry) => keeps(filter, entry));
  const hidden = entries.length - kept.length;
  const listed = showAll ? entries : kept;
  const shown = listed.slice(0, DAY_LOG_LINES);
  const earlier = listed.length - shown.length;
  const keys = keysOf(shown);
  return (
    <aside aria-labelledby={preview ? undefined : heading} aria-label={preview ? "Today's log preview" : undefined}
      className={`min-w-0 h-full overflow-y-auto [scrollbar-width:thin] day-log-scroll ${className}`}>
      <div className={`sticky top-0 z-10 flex min-h-11 items-center gap-2 py-1 ${preview ? "bg-paper" : "bg-canvas"}`}>
        <Heading id={heading} className="mr-auto font-sans text-[11px] font-semibold uppercase leading-none tracking-[0.06em] text-ink-3">
          Today's log
        </Heading>
        {preview && <span className="font-sans text-[13.5px] text-ink">{kept.length} of {entries.length}</span>}
        {!preview && hidden > 0 && (
          <button type="button" aria-pressed={showAll} onClick={() => setShowAll(!showAll)}
            className={`min-h-8 px-1.5 font-sans text-[13.5px] ${showAll ? "bg-act-wash text-act" : "text-ink hover:text-act"}`}>
            {showAll ? "Highlights only" : `Show all (${hidden} hidden)`}
          </button>
        )}
        {!preview && <Link to="/settings/$tab" params={{ tab: "log" }} search={keepScope} aria-label="Filter Today's log" title="Filter Today's log"
          className="flex size-8 shrink-0 items-center justify-center border border-rule text-ink-2 hover:border-ink-3 hover:text-ink">
          <SettingsIcon />
        </Link>}
      </div>
      {shown.length === 0 ? (
        <p className="font-mono text-[12.5px] text-ink-3">{entries.length === 0 ? "Nothing logged since midnight." : "Nothing in the filter since midnight."}</p>
      ) : (
        <ol className="ml-1 border-l border-rule pt-1 font-mono text-[13px] leading-[1.5] text-ink-2">
          {shown.map((entry, index) => (
            <DayLogEntry key={keys[index]} entry={entry} quiet={!keeps(filter, entry)}
              colour={entry.project == null ? undefined : colours.get(projectName(entry.project))} />
          ))}
          {earlier > 0 && <li className="py-[7px] pl-4 text-ink-3">and {earlier} earlier today</li>}
        </ol>
      )}
    </aside>
  );
}
