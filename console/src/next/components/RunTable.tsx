import { Link } from "@tanstack/react-router";
import { formatClock, formatSettled } from "../../lib/format";
import { localMidnight } from "../../lib/ledger";
import { classicHref } from "../../lib/view";
import type { RunRow } from "../history";
import { runPage } from "../run";
import { keepScope, useConsolePeers } from "../scope";
import { STATES, type State } from "../state";
import { StateDot, TONE_TEXT } from "./StateDot";

const GRID = "grid grid-cols-[84px_100px_minmax(0,2.6fr)_96px_minmax(0,1.7fr)_64px_96px_96px_80px] items-start gap-3";
const MONO = "font-mono text-[12px] leading-[1.4] text-ink-3";
const DASH = "–";
const NEEDS_YOU = new Set<State>(["stale", "question", "paused", "ready_to_merge"]);

function started(ms: number, now: number): string {
  return ms < localMidnight(now) ? `yest ${formatClock(ms)}` : formatClock(ms);
}

function prNumber(url: string | null): string {
  const segment = url?.replace(/\/+$/, "").split("/").pop() ?? "";
  return /^\d+$/.test(segment) ? `#${segment}` : DASH;
}

function RunTableRow({ row, now }: { row: RunRow; now: number }) {
  const start = started(row.started_ms, now);
  const quiet = row.state === "merged" || row.state === "abandoned";
  const need = NEEDS_YOU.has(row.state);
  const host = useConsolePeers().hosts.find((each) => each.base === row.base);
  const target = host?.status == null ? null : runPage(host.status.project, row.id);
  return (
    <li
      data-run={row.id}
      data-need={need ? "" : undefined}
      className={`${GRID} relative border-b border-rule-2 p-3 text-[14px] transition-colors duration-(--motion-colour) hover:bg-paper-2 ${quiet ? "text-ink-2" : "text-ink"} ${need ? "bg-need-wash shadow-[inset_2px_0_0_var(--need)]" : ""}`}
    >
      {target ? (
        <Link
          {...target}
          aria-label={`Run ${row.id}, ${row.ticket} ${row.title}, ${row.project}, ${row.stage}, started ${start}`}
          className={`${MONO} text-ink-2 no-underline after:absolute after:inset-0 focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-act`}
        >
          {row.id}
        </Link>
      ) : (
        <a
          href={classicHref(`#run=${row.id}&daemon=${encodeURIComponent(row.base)}`)}
          aria-label={`Run ${row.id}, ${row.ticket} ${row.title}, ${row.project}, ${row.stage}, started ${start}`}
          className={`${MONO} text-ink-2 no-underline after:absolute after:inset-0 focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-act`}
        >
          {row.id}
        </a>
      )}
      <span className="font-mono text-[12px] font-medium leading-[1.4] tabular-nums text-ink-3 [overflow-wrap:anywhere]">
        <Link
          to="/tickets/$project/$key"
          params={{ project: row.project, key: row.ticket }}
          search={keepScope}
          className="relative no-underline hover:text-ink hover:underline"
        >
          {row.ticket}
        </Link>
      </span>
      <span className={`min-w-0 font-sans font-semibold ${quiet ? "text-ink-2" : "text-ink"}`}>{row.title}</span>
      <span className={`${MONO} [overflow-wrap:anywhere]`}>{row.project}</span>
      <span className="inline-flex items-start gap-1.5">
        <StateDot state={row.state} hideWord />
        <span className={`font-sans text-[13px] font-medium leading-[1.3] ${row.state === "merged" ? "text-ink" : TONE_TEXT[STATES[row.state].tone]}`}>{row.stage}</span>
      </span>
      <span className={MONO}>{row.rounds > 0 ? row.rounds : DASH}</span>
      <span className={MONO}>{prNumber(row.pr_url)}</span>
      <span className={`${MONO} whitespace-nowrap`}>{start}</span>
      <span className={`${MONO} whitespace-nowrap text-right`}>{formatSettled(row.elapsed_ms)}</span>

    </li>
  );
}

/** The runs as one table that scrolls sideways inside its own box. */
export function RunTable({ rows, now, empty }: { rows: RunRow[]; now: number; empty: string }) {
  return (
    <div data-run-table className="overflow-x-auto border border-rule bg-paper">
      <div className="min-w-[1100px]">
        <div aria-hidden="true" className={`${GRID} border-b border-rule px-3 pb-2.5 pt-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3`}>
          <span>Run</span>
          <span>Ticket</span>
          <span>Title</span>
          <span>Project</span>
          <span>State</span>
          <span>Rounds</span>
          <span>PR</span>
          <span>Started</span>
          <span className="text-right">Took</span>
        </div>
        <ol aria-label="Runs, newest first" className="m-0 list-none p-0">
          {rows.map((row) => (
            <RunTableRow key={`${row.base} ${row.id}`} row={row} now={now} />
          ))}
        </ol>
        {rows.length === 0 && <p className="m-0 px-3 py-[18px] font-mono text-[13px] leading-[1.4] text-ink-3">{empty}</p>}
      </div>
    </div>
  );
}
