import { PrLink, Sha } from "../../components/ShippedTable";
import { formatClock } from "../../lib/format";
import type { ShippedRow } from "../../lib/types";
import { StateDot } from "./StateDot";

const roundsOf = (rounds: number) => `${rounds} ${rounds === 1 ? "round" : "rounds"}`;

/** Today's merges, newest first, as `useOverview()` reads them. */
export function MergedToday({ rows }: { rows: ShippedRow[] }) {
  return (
    <ul className="border border-rule bg-paper">
      {rows.map((row) => (
        <li
          key={`${row.daemon ?? ""}:${row.id}`}
          data-merge={row.ticket}
          className="grid min-h-[54px] grid-cols-[18px_84px_minmax(0,1fr)_48px] items-center gap-x-3 gap-y-1.5 px-4 py-3.5 sm:gap-x-5 sm:px-5 font-mono text-[12px] text-ink-3 [&+&]:border-t [&+&]:border-rule-2"
        >
          <StateDot state="merged" hideWord />
          <span className="text-ink-2">{row.ticket}</span>
          <span className="flex min-w-0 flex-col gap-1">
            <b className="font-sans text-[14px] font-medium break-words text-ink sm:truncate">
              {row.title ?? row.ticket}
            </b>
            <span>
              {row.pr_url ? <PrLink url={row.pr_url} title={row.merge_sha} /> : <Sha row={row} />} · {roundsOf(row.rounds)}
            </span>
          </span>
          <span className="text-right">{formatClock(row.ended_ms)}</span>
        </li>
      ))}
    </ul>
  );
}
