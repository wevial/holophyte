import type { TurnFilter } from "../turn";
import { Btn } from "./Btn";
import { TranscriptIcon } from "./icons";

export function TurnToolbar({ query, onQuery, filter, onFilter, counts, matches, matchingEntries, expanded, onExpand, onJump }: {
  query: string; onQuery: (query: string) => void; filter: TurnFilter; onFilter: (filter: TurnFilter) => void;
  counts: Record<TurnFilter, number>; matches: number; matchingEntries: number;
  expanded: boolean; onExpand: () => void; onJump: () => void;
}) {
  const entries = (n: number) => `${n} ${n === 1 ? "entry" : "entries"}`;
  return <div className="mb-3 flex min-w-0 flex-wrap items-center gap-3 border border-rule bg-paper p-3">
    <label className="flex min-w-0 basis-72 grow items-center gap-2 border border-rule bg-paper-2 px-3 text-ink-3">
      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" className="shrink-0"><circle cx="6.5" cy="6.5" r="4.5" /><path d="m10 10 4 4" /></svg>
      <input type="search" aria-label="Search this turn" placeholder="Search text, commands and output" value={query} onChange={event => onQuery(event.target.value)} className="min-w-0 w-full bg-transparent py-2 font-mono text-[12px] text-ink outline-none" />
    </label>
    <span aria-live="polite" className="font-mono text-[12px] text-ink-3">{query.trim() ? `${matches} ${matches === 1 ? "match" : "matches"} in ${entries(matchingEntries)}` : `${entries(counts.All)} · ${counts.Errors} ${counts.Errors === 1 ? "error" : "errors"}`}</span>
    <div role="group" aria-label="Show" className="flex flex-wrap border border-rule">
      {(["All", "Said", "Tools", "Errors"] as const).map(kind => <button type="button" key={kind} aria-pressed={filter === kind} onClick={() => onFilter(kind)} className={`px-3 py-2 font-mono text-[11px] uppercase ${filter === kind ? "bg-ink text-paper" : "text-ink-3 hover:text-ink"}`}>{kind} {counts[kind]}</button>)}
    </div>
    <Btn disabled={counts.Errors === 0} onClick={onJump}><span className="text-bad"><TranscriptIcon kind="error" /></span>Jump to first error</Btn>
    <Btn onClick={onExpand}>{expanded ? "Collapse all" : "Expand all"}</Btn>
  </div>;
}
