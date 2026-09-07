import { PILL_TEXT } from "../lib/attention";
import { formatAge, formatClock } from "../lib/format";
import { longest, median, type ResolvedRow } from "../lib/resolved";
import { KindPill } from "./KindPill";

/** The shift's receipt under the band: a strip counting what was cleared
 *  since local midnight, opening to one row per intervention. */
export function ResolvedFold({ rows, open, onToggle }: { rows: ResolvedRow[]; open: boolean; onToggle: () => void }) {
  const mid = median(rows);
  const long = longest(rows);
  return (
    <section data-resolved aria-label="Resolved today" className="border-y border-line bg-card-header">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="flex w-full items-baseline gap-3 px-6 py-[9px] text-left"
      >
        <span aria-hidden="true" className="text-muted">
          {open ? "▾" : "▸"}
        </span>
        <span data-resolved-count className="text-[12px] font-semibold text-ink">
          Resolved today · {rows.length}
        </span>
        {mid != null && long != null && (
          <span data-resolved-waits className="text-[12px] text-muted">
            median wait {formatAge(mid)} · longest {formatAge(long)}
          </span>
        )}
      </button>
      {open &&
        (rows.length === 0 ? (
          <p className="px-6 pb-3 text-[13px] text-muted">Nothing resolved yet today</p>
        ) : (
          <ol className="list-none">
            {rows.map((row, index) => (
              <li
                key={`${row.at}:${index}`}
                data-resolved-row
                data-kind={row.kind}
                className="grid grid-cols-[96px_84px_1fr_80px_60px_50px] items-start gap-[14px] border-t border-line-faint px-6 py-[9px]"
              >
                <div>
                  <KindPill kind={row.kind}>{PILL_TEXT[row.kind]}</KindPill>
                </div>
                <span className="truncate font-mono text-[12px] font-semibold text-ink">{row.ticket ?? "—"}</span>
                <span className="min-w-0 break-words text-[13px] text-body">{row.text}</span>
                <span data-waited className="font-mono text-[11px] text-muted">
                  {row.waited_ms == null ? "" : `waited ${formatAge(row.waited_ms)}`}
                </span>
                <span className="text-[12px] text-muted">{row.by}</span>
                <span className="font-mono text-[11px] text-muted">{formatClock(row.at)}</span>
              </li>
            ))}
          </ol>
        ))}
    </section>
  );
}
