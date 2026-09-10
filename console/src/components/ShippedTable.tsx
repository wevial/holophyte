import { useState, type KeyboardEvent } from "react";
import { formatClock } from "../lib/format";
import type { Fetch } from "../lib/poll";
import {
  boxFill,
  boxRatio,
  commitLink,
  deltaLabel,
  deltaTone,
  groupByDay,
  minutesLabel,
  prLabel,
  shortSha,
  withinDays,
  type DayGroup,
} from "../lib/shipped";
import type { ShippedRow } from "../lib/types";
import { RunDetail } from "./RunDetail";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** Nine columns: the chevron, then the eight the headers name. */
const GRID = "grid grid-cols-[20px_56px_90px_1fr_70px_60px_70px_200px_80px] items-center gap-3 px-4";
/** The chevron is the row's `::before` so the eight cells stay its only
 *  children: "▸" shut, "▾" while `aria-expanded`. */
const CHEVRON = "before:text-[12px] before:text-faint before:content-['▸'] aria-expanded:before:content-['▾']";
const COLUMNS = ["Merged", "Ticket", "Title", "Project", "Rounds", "Findings", "Actual vs time box", "Change"];
const FILLS = { ok: "bg-ok", warn: "bg-warn", over: "bg-bad" };
const DELTA = { ok: "text-ok-text", over: "text-bad-text", muted: "text-muted" };

/** Actual against the time box: a 6px bar green at or under 80 %, amber
 *  above, red past the box, then "18m / 30m" and the signed delta. */
export function ActualVsBox({ actualMin, estimateMin }: { actualMin: number; estimateMin: number | null }) {
  const tone = boxRatio(actualMin, estimateMin);
  const delta = deltaLabel(actualMin, estimateMin);
  return (
    <span className="flex items-center gap-2">
      <span
        role="progressbar"
        aria-valuenow={Math.round(boxFill(actualMin, estimateMin) * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        data-tone={tone ?? "none"}
        className="block h-[6px] w-[92px] shrink-0 overflow-hidden rounded-[3px] bg-track"
      >
        {tone && (
          <span className={`block h-full ${FILLS[tone]}`} style={{ width: `${boxFill(actualMin, estimateMin) * 100}%` }} />
        )}
      </span>
      <span data-minutes className="font-mono text-[11px] text-muted">
        {minutesLabel(actualMin, estimateMin)}
      </span>
      {delta && (
        <span data-delta={deltaTone(actualMin, estimateMin)} className={`font-mono text-[11px] ${DELTA[deltaTone(actualMin, estimateMin)]}`}>
          {delta}
        </span>
      )}
    </span>
  );
}

const SHA_CLASS = "font-mono text-[12px] text-link";

/** The short sha in the mono teal style: an anchor to the commit on origin
 *  opening in a new tab where the daemon served `commit_url`, else plain. */
export function Sha({ row }: { row: { merge_sha: string | null; commit_url?: string | null } }) {
  const href = commitLink(row);
  const text = shortSha(row.merge_sha);
  if (href == null) {
    return (
      <span data-sha className={SHA_CLASS}>
        {text}
      </span>
    );
  }
  return (
    <a data-sha href={href} target="_blank" rel="noopener noreferrer" className={`${SHA_CLASS} hover:underline`}>
      {text}
    </a>
  );
}

/** The run's pull request as "PR #N" in the sha's link style, opening in
 *  a new tab; nothing at all when the run opened none. `title` is the
 *  anchor's tooltip: the Shipped table hands it the merge sha the cell no
 *  longer shows. */
export function PrLink({ url, title }: { url: string | null | undefined; title?: string | null }) {
  if (!url) return null;
  return (
    <a data-pr href={url} title={title ?? undefined} target="_blank" rel="noopener noreferrer" className={`${SHA_CLASS} hover:underline`}>
      {prLabel(url)}
    </a>
  );
}

/** One merge, a toggle like a Now row: clicking expands the run's detail
 *  card beneath, read from the daemon that merged it. The row is a
 *  `role="button"` div, not a button, because the Change cell is an anchor;
 *  a click or Enter on it follows the link without toggling the row. The
 *  Change cell is the pull request when the run had one, its merge sha as
 *  the tooltip, else the commit's short sha. */
function Row({
  row,
  expanded,
  onToggle,
  now,
  polls,
  deps,
}: {
  row: ShippedRow;
  expanded: boolean;
  onToggle: () => void;
  now: number;
  polls: number;
  deps?: { fetch: Fetch };
}) {
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // Only keys aimed at the row itself toggle it: Enter on the focused sha
    // anchor must follow the link, not bubble up and be swallowed here.
    if (event.target !== event.currentTarget) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onToggle();
  };
  return (
    <div className="border-t border-line-faint">
      <div
        role="button"
        tabIndex={0}
        data-row={row.id}
        aria-expanded={expanded}
        onClick={onToggle}
        onKeyDown={onKeyDown}
        className={`${GRID} ${CHEVRON} cursor-pointer py-[11px] hover:bg-hover`}
      >
        <span className="font-mono text-[12px] text-muted">{formatClock(row.ended_ms)}</span>
        <span className="truncate font-mono text-[13px] font-semibold text-ink">{row.ticket}</span>
        <span className="truncate text-[13px] text-body">{row.title ?? ""}</span>
        <span className="truncate text-[13px] text-muted">{row.project}</span>
        <span className="font-mono text-[13px] text-body">{row.rounds}</span>
        <span className="font-mono text-[13px] text-body">{row.findings}</span>
        <ActualVsBox actualMin={row.actual_min} estimateMin={row.estimate_min} />
        <span onClick={(event) => event.stopPropagation()} className="flex items-baseline">
          {row.pr_url ? <PrLink url={row.pr_url} title={row.merge_sha} /> : <Sha row={row} />}
        </span>
      </div>
      {expanded && <RunDetail base={row.daemon ?? ""} id={row.id} now={now} polls={polls} deps={deps} />}
    </div>
  );
}

function DayHeader({ group }: { group: DayGroup }) {
  return (
    <div data-day-header className="flex items-baseline gap-2 border-t border-line-faint bg-card-header px-4 py-2">
      <span className="text-[12px] font-semibold text-ink">{group.label}</span>
      <span className="font-mono text-[11px] text-faint">{plural(group.rows.length, "merge")}</span>
    </div>
  );
}

/**
 * The merge ledger: `rows` under a sub-header per local day of `ended_ms`,
 * newest first, eight columns each. `now` is the daemon's clock and names
 * "Today"; `days` keeps only the last so many calendar days (1 is today
 * alone) so the Board can mount today's group; `tz` pins the zone for tests.
 * One row at a time expands to its `RunDetail`, keyed by daemon and id so
 * a poll that replaces `rows` leaves it open; `polls` re-reads the detail.
 */
export function ShippedTable({
  rows,
  now,
  polls = 0,
  deps,
  days,
  tz,
}: {
  rows: ShippedRow[];
  now: number;
  polls?: number;
  deps?: { fetch: Fetch };
  days?: number;
  tz?: string;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const grouped = groupByDay(rows, now, tz);
  const groups = days == null ? grouped : withinDays(grouped, days);
  return (
    <div className="overflow-hidden rounded-[10px] border border-line bg-card shadow-card">
      <div className={`${GRID} py-2 text-[11px] font-semibold uppercase tracking-[.08em] text-faint`}>
        <span aria-hidden="true" />
        {COLUMNS.map((column) => (
          <span key={column}>{column}</span>
        ))}
      </div>
      {groups.map((group) => (
        <div key={group.key} data-day={group.key}>
          <DayHeader group={group} />
          {group.rows.map((row) => {
            const key = `${row.daemon ?? ""}#${row.id}`;
            return (
              <Row
                key={key}
                row={row}
                expanded={expanded === key}
                onToggle={() => setExpanded((current) => (current === key ? null : key))}
                now={now}
                polls={polls}
                deps={deps}
              />
            );
          })}
        </div>
      ))}
    </div>
  );
}
