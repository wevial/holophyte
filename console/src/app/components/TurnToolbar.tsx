import type { TurnFilter } from "../turn";
import { Btn } from "./Btn";
import { Segmented } from "./Segmented";
import { TranscriptIcon } from "./icons";

const FILTERS = ["All", "Said", "Tools", "Errors"] as const;

export function TurnToolbar({ query, onQuery, filter, onFilter, counts, matches, matchingEntries, expanded, onExpand, onJump }: {
  query: string; onQuery: (query: string) => void; filter: TurnFilter; onFilter: (filter: TurnFilter) => void;
  counts: Record<TurnFilter, number>; matches: number; matchingEntries: number;
  expanded: boolean; onExpand: () => void; onJump: () => void;
}) {
  const entries = (n: number) => `${n} ${n === 1 ? "entry" : "entries"}`;
  return <div className="mb-3 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
    <label className="flex h-[30px] min-w-0 basis-72 grow items-center gap-2 rounded-button bg-selected px-2.5 text-ink-3 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus">
      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" className="size-4 shrink-0"><circle cx="6.5" cy="6.5" r="4.5" /><path d="m10 10 4 4" /></svg>
      <input type="search" aria-label="Search this turn" placeholder="Search text, commands and output" value={query} onChange={event => onQuery(event.target.value)}
        className="h-full min-w-0 flex-1 border-0 bg-transparent font-sans text-[14px] text-ink outline-none! placeholder:text-ink-3" />
    </label>
    <span aria-live="polite" className="font-sans text-[13px] text-ink-3">{query.trim() ? `${matches} ${matches === 1 ? "match" : "matches"} in ${entries(matchingEntries)}` : `${entries(counts.All)} · ${counts.Errors} ${counts.Errors === 1 ? "error" : "errors"}`}</span>
    <div className="min-w-0 max-w-full [&>[role=group]]:max-w-full [&>[role=group]]:flex-wrap">
      <Segmented label="Show" options={FILTERS.map(kind => ({ value: kind, label: `${kind} ${counts[kind]}` }))} value={filter} onPick={onFilter} />
    </div>
    <Btn disabled={counts.Errors === 0} onClick={onJump}><span className="text-bad"><TranscriptIcon kind="error" /></span>Jump to first error</Btn>
    <Btn onClick={onExpand}>{expanded ? "Collapse all" : "Expand all"}</Btn>
  </div>;
}
