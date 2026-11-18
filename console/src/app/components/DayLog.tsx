import { useId, useLayoutEffect, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import type { LedgerEntry } from "../../lib/contract.gen";
import { projectName } from "../../lib/derive";
import { formatClock } from "../../lib/format";
import { prLabel } from "../../lib/shipped";
import { categoryOf, keeps, phraseOf, projectColours, useDayLogFilter, type DayLogFilter } from "../dayLog";
import { runPage } from "../run";
import { keepScope, useConsolePeers } from "../scope";
import { SettingsIcon } from "./icons";
import { STATE_ICONS } from "./StateDot";

const DAY_LOG_LINES = 25;

export type LineTone = "live" | "bad" | "act" | "ask" | "wait" | "ink-3";

export const MARK: Record<LineTone, string> = {
  live: "bg-live",
  bad: "bg-bad",
  act: "bg-act",
  ask: "bg-ask",
  wait: "bg-wait",
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

export const LINE = "relative before:absolute before:inset-y-0 before:-left-px before:w-px first:before:top-3 not-last:before:bg-rule last:before:bg-linear-to-b last:before:from-rule last:before:to-transparent";

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

const GLYPHS: Partial<Record<LineTone, ReactNode>> = {
  live: STATE_ICONS.merged,
  bad: STATE_ICONS.failed,
  ask: STATE_ICONS.needs_you,
  act: STATE_ICONS.todo,
};

const GLYPH_TEXT: Record<LineTone, string> = { live: "text-live", bad: "text-bad", act: "text-act", ask: "text-ask-ink", wait: "text-wait", "ink-3": "text-ink-3" };

function Glyph({ tone }: { tone: LineTone }) {
  const icon = GLYPHS[tone];
  return (
    <span data-tone={tone} aria-hidden="true" className={`relative flex size-4 items-center justify-center bg-canvas ${GLYPH_TEXT[tone]}`}>
      {icon == null ? (
        <span className="block size-1 rounded-full bg-current" />
      ) : (
        <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="size-3.5">
          {icon}
        </svg>
      )}
    </span>
  );
}

const ROW = "grid min-h-8 grid-cols-[40px_16px_minmax(0,1fr)] gap-x-2.5";

/** A row's glyph over the log's connecting line: from the first row's glyph
 *  down, solid beside each row and fading beside the last. */
function RailCell({ tone, first, last }: { tone?: LineTone; first: boolean; last: boolean }) {
  const fill = last ? "bg-linear-to-b from-rule to-transparent" : "bg-rule";
  return (
    <span className="relative flex justify-center self-stretch pt-2">
      <span data-rail aria-hidden="true" className={`absolute bottom-0 left-1/2 w-px -translate-x-1/2 ${first ? "top-4" : "top-0"} ${fill}`} />
      {tone != null && <Glyph tone={tone} />}
    </span>
  );
}

function DayLogEntry({ entry, quiet, colour, first, last }: { entry: LedgerEntry; quiet: boolean; colour: string | undefined; first: boolean; last: boolean }) {
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
    <li data-quiet={quiet || undefined} className={`${ROW} ${quiet ? "text-ink-3" : ""}`}>
      <time dateTime={new Date(entry.at).toISOString()} className="py-[7px] font-mono text-[12px] leading-[18px] text-ink-3">
        {formatClock(entry.at)}
      </time>
      <RailCell tone={tone} first={first} last={last} />
      <div className="min-w-0 py-[7px] leading-[18px]">
        <span className="flex flex-wrap items-baseline gap-x-1.5">
          <span className={quiet ? "opacity-75" : "text-ink"}>
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
          <pre className="mt-1.5 max-h-[calc(16*1.45em)] overflow-y-auto whitespace-pre-wrap break-words border-l border-rule pl-2 font-mono text-[12px] text-ink-3">
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
  const rows = shown.length + (earlier > 0 ? 1 : 0);
  const [stuck, setStuck] = useState(false);
  return (
    <aside aria-labelledby={preview ? undefined : heading} aria-label={preview ? "Today's log preview" : undefined}
      onScroll={(event) => setStuck(event.currentTarget.scrollTop > 0)}
      className={`min-w-0 h-full overflow-y-auto [scrollbar-width:thin] day-log-scroll ${className}`}>
      <div data-stuck={stuck || undefined} className={`sticky top-0 z-10 flex min-h-11 items-center gap-2 py-1 ${stuck ? "bg-canvas" : ""}`}>
        <Heading id={heading} className="mr-auto font-sans text-[14px] font-semibold leading-none text-ink">
          {preview ? "Today's log" : "Today"}
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
        <ol className="pt-1 font-sans text-[13px] text-ink-2">
          {shown.map((entry, index) => (
            <DayLogEntry key={keys[index]} entry={entry} quiet={!keeps(filter, entry)} first={index === 0} last={index === rows - 1}
              colour={entry.project == null ? undefined : colours.get(projectName(entry.project))} />
          ))}
          {earlier > 0 && (
            <li className={`${ROW} text-ink-3`}>
              <span />
              <RailCell first={false} last />
              <span className="py-[7px] leading-[18px]">and {earlier} earlier today</span>
            </li>
          )}
        </ol>
      )}
    </aside>
  );
}
