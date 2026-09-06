import { formatClock } from "../lib/format";
import {
  boxFill,
  boxRatio,
  deltaLabel,
  deltaTone,
  groupByDay,
  minutesLabel,
  shortSha,
  withinDays,
  type DayGroup,
} from "../lib/shipped";
import type { ShippedRow } from "../lib/types";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

const GRID = "grid grid-cols-[56px_90px_1fr_70px_60px_70px_200px_80px] items-center gap-3 px-4";
const COLUMNS = ["Merged", "Ticket", "Title", "Project", "Rounds", "Findings", "Actual vs time box", "SHA"];
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

function Row({ row }: { row: ShippedRow }) {
  return (
    <div data-row={row.id} className={`${GRID} border-t border-line-faint py-[11px] hover:bg-hover`}>
      <span className="font-mono text-[12px] text-muted">{formatClock(row.ended_ms)}</span>
      <span className="truncate font-mono text-[13px] font-semibold text-ink">{row.ticket}</span>
      <span className="truncate text-[13px] text-body">{row.title ?? ""}</span>
      <span className="truncate text-[13px] text-muted">{row.host ?? ""}</span>
      <span className="font-mono text-[13px] text-body">{row.rounds}</span>
      <span className="font-mono text-[13px] text-body">{row.findings}</span>
      <ActualVsBox actualMin={row.actual_min} estimateMin={row.estimate_min} />
      <span data-sha className="font-mono text-[12px] text-link">
        {shortSha(row.merge_sha)}
      </span>
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
 */
export function ShippedTable({
  rows,
  now,
  days,
  tz,
}: {
  rows: ShippedRow[];
  now: number;
  days?: number;
  tz?: string;
}) {
  const grouped = groupByDay(rows, now, tz);
  const groups = days == null ? grouped : withinDays(grouped, days);
  return (
    <div className="overflow-hidden rounded-[10px] border border-line bg-card shadow-card">
      <div className={`${GRID} py-2 text-[11px] font-semibold uppercase tracking-[.08em] text-faint`}>
        {COLUMNS.map((column) => (
          <span key={column}>{column}</span>
        ))}
      </div>
      {groups.map((group) => (
        <div key={group.key} data-day={group.key}>
          <DayHeader group={group} />
          {group.rows.map((row) => (
            <Row key={row.id} row={row} />
          ))}
        </div>
      ))}
    </div>
  );
}
