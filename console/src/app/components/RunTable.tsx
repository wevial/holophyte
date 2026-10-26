import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { formatClock, formatSettled } from "../../lib/format";
import { localMidnight } from "../../lib/ledger";
import type { RunOrder, RunRow, RunSort } from "../history";
import { runPage } from "../run";
import { keepScope, useConsolePeers } from "../scope";
import { STATES, type State } from "../state";
import { StateDot, TONE_TEXT } from "./StateDot";
import { plainTitle, TitleText } from "./TitleText";

const GRID = "grid grid-cols-[84px_100px_minmax(0,2.6fr)_96px_minmax(0,1.7fr)_64px_96px_96px_96px_80px] items-start gap-3";
const MONO = "font-mono text-[12px] leading-[1.4] text-ink-3";
const DASH = "–";
const NEEDS_YOU = new Set<State>(["stale", "question", "paused", "ready_to_merge"]);

function clock(ms: number, now: number): string {
  return ms < localMidnight(now) ? `yest ${formatClock(ms)}` : formatClock(ms);
}

export function prNumber(url: string | null): string {
  const segment = url?.replace(/\/+$/, "").split("/").pop() ?? "";
  return /^\d+$/.test(segment) ? `#${segment}` : DASH;
}

export function RunRowLink({ row, ...props }: {
  row: { base: string; id: number };
  "aria-label": string;
  className: string;
  children: React.ReactNode;
}) {
  const host = useConsolePeers().hosts.find((each) => each.base === row.base);
  const target = host?.status == null ? null : runPage(host.status.project, row.id);
  return target ? <Link {...target} {...props} /> : <span className={props.className}>{props.children}</span>;
}

export function ResumedAs({ row }: { row: { base: string; resumed_as?: number | null } }) {
  if (row.resumed_as == null) return null;
  return (
    <>
      resumed as{" "}
      <RunRowLink row={{ base: row.base, id: row.resumed_as }} aria-label={`run ${row.resumed_as}`} className="relative underline underline-offset-2 hover:text-ink">
        run {row.resumed_as}
      </RunRowLink>
    </>
  );
}

function RunTableRow({ row, now }: { row: RunRow; now: number }) {
  const start = clock(row.started_ms, now);
  const quiet = row.state === "merged" || row.state === "abandoned" || row.state === "resumed";
  const need = NEEDS_YOU.has(row.state);
  return (
    <li
      data-row
      data-run={row.id}
      data-need={need ? "" : undefined}
      className={`${GRID} relative border-b border-rule-2 p-3 text-[14px] transition-colors duration-(--motion-colour) hover:bg-paper-2 ${quiet ? "text-ink-2" : "text-ink"} ${need ? "bg-need-wash shadow-[inset_2px_0_0_var(--need)]" : ""}`}
    >
      <RunRowLink row={row} aria-label={`Run ${row.id}, ${row.ticket} ${plainTitle(row.title)}, ${row.project}, ${row.stage}, started ${start}`} className={`${MONO} text-ink-2 no-underline after:absolute after:inset-0 focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-focus`}>
        {row.id}
      </RunRowLink>
      <span className="font-mono text-[11.5px] font-medium leading-[1.3] tabular-nums text-ink-3 [overflow-wrap:anywhere]">
        <Link
          to="/tickets/$project/$key"
          params={{ project: row.project, key: row.ticket }}
          search={keepScope}
          className="relative no-underline hover:text-ink hover:underline"
        >
          {row.ticket}
        </Link>
      </span>
      <span className={`min-w-0 font-read text-[15px] font-medium leading-[1.35] ${quiet ? "text-ink-2" : "text-ink"}`}><TitleText>{row.title}</TitleText></span>
      <span className={`${MONO} [overflow-wrap:anywhere]`}>{row.project}</span>
      <span className="inline-flex items-start gap-1.5">
        <StateDot state={row.state} hideWord />
        <span className={`font-sans text-[13px] font-medium leading-[1.3] ${row.state === "merged" ? "text-ink" : TONE_TEXT[STATES[row.state].tone]}`}>
          {row.state === "resumed" ? <>paused · <ResumedAs row={row} /></> : row.stage}
        </span>
      </span>
      <span className={MONO}>{row.rounds > 0 ? row.rounds : DASH}</span>
      <span className={MONO}>{prNumber(row.pr_url)}</span>
      <span className={`${MONO} whitespace-nowrap`}>{start}</span>
      <span className={`${MONO} whitespace-nowrap`}>{row.ended_ms == null ? DASH : clock(row.ended_ms, now)}</span>
      <span className={`${MONO} whitespace-nowrap text-right`}>{formatSettled(row.elapsed_ms)}</span>
    </li>
  );
}

const HEADS: { label: string; sort?: RunSort; right?: boolean }[] = [
  { label: "Run" },
  { label: "Ticket" },
  { label: "Title" },
  { label: "Project" },
  { label: "State", sort: "state" },
  { label: "Rounds", sort: "rounds" },
  { label: "PR" },
  { label: "Started", sort: "started" },
  { label: "Ended", sort: "ended" },
  { label: "Took", sort: "duration", right: true },
];

function SortArrow({ order }: { order: RunOrder | null }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 8 10" width={8} height={10} fill="none" stroke="currentColor" strokeWidth={1.4} className={order ? "text-act" : "opacity-50"}>
      {order === "desc" && <path d="M4 1v8M1 6l3 3 3-3" />}
      {order === "asc" && <path d="M4 9V1M1 4l3-3 3 3" />}
      {order == null && <path d="M4 1v8M2 3l2-2 2 2M2 7l2 2 2-2" />}
    </svg>
  );
}

export function RunTableHeads({ sort, order, onSort }: { sort: RunSort; order: RunOrder; onSort: (key: RunSort) => void }) {
  return (
    <div data-run-heads className={`${GRID} relative z-[1] border border-rule bg-paper px-3 pb-2.5 pt-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3`}>
      {HEADS.map((head) =>
        head.sort ? (
          <button
            key={head.label}
            type="button"
            aria-label={`Sort by ${head.label.toLowerCase()}`}
            aria-pressed={sort === head.sort}
            onClick={() => onSort(head.sort!)}
            className={`inline-flex cursor-pointer items-center gap-1.5 uppercase tracking-[0.12em] hover:text-ink aria-pressed:text-ink ${head.right ? "justify-self-end" : "justify-self-start"}`}
          >
            {head.label}
            <SortArrow order={sort === head.sort ? order : null} />
          </button>
        ) : (
          <span key={head.label}>{head.label}</span>
        ),
      )}
    </div>
  );
}

/** The runs as one table that scrolls sideways inside its own box while it
 *  is narrower than the table; `dock` sits above the rows inside that box. */
export function RunTable({ rows, now, empty, label, dock }: { rows: RunRow[]; now: number; empty: string; label: string; dock?: ReactNode }) {
  return (
    <div data-run-table className="overflow-x-auto @min-[1100px]:overflow-x-visible">
      <div className="min-w-[1100px]">
        {dock}
        <div className="border-x border-b border-rule bg-paper">
          <ol aria-label={label} className="m-0 list-none p-0">
            {rows.map((row) => (
              <RunTableRow key={`${row.base} ${row.id}`} row={row} now={now} />
            ))}
          </ol>
          {rows.length === 0 && <p className="m-0 px-3 py-[18px] font-mono text-[13px] leading-[1.4] text-ink-3">{empty}</p>}
        </div>
      </div>
    </div>
  );
}
