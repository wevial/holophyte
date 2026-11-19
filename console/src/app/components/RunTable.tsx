import { useId, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { formatClock, formatSettled, formatStamp } from "../../lib/format";
import { localMidnight } from "../../lib/ledger";
import { daysAgo } from "../../lib/shipped";
import { projectColours } from "../dayLog";
import type { RunRow } from "../history";
import { runPage } from "../run";
import { keepScope, useConsolePeers } from "../scope";
import type { State } from "../state";
import { StateDot } from "./StateDot";
import { plainTitle, TitleText } from "./TitleText";
import { ACTIONS_OFF } from "../../lib/actions";
import { MoreMenu, ROW_MENU } from "./MoreMenu";
import { usePlaceItems, useRevertFlow } from "./Revert";

const GRID = "grid items-center gap-x-3 max-[760px]:grid-cols-[22px_auto_auto_minmax(0,1fr)_auto] max-[760px]:gap-x-1.5";
const DAY_COLUMNS = "grid-cols-[16px_36px_96px_minmax(0,1fr)_minmax(0,148px)_44px_44px_48px_60px] min-[760px]:@max-[780px]:grid-cols-[16px_36px_96px_minmax(0,1fr)_44px_44px_48px_60px] min-[760px]:@max-[600px]:grid-cols-[16px_36px_96px_minmax(0,1fr)_48px_60px]";
const DATED_COLUMNS = "grid-cols-[16px_36px_96px_minmax(0,1fr)_minmax(0,148px)_44px_44px_88px_60px] min-[760px]:@max-[780px]:grid-cols-[16px_36px_96px_minmax(0,1fr)_44px_44px_88px_60px] min-[760px]:@max-[600px]:grid-cols-[16px_36px_96px_minmax(0,1fr)_88px_60px]";
const MONO = "font-mono text-[12px] leading-none tabular-nums text-ink-3";
const WIDE = "max-[760px]:hidden";
const ROOMY = "max-[760px]:hidden min-[760px]:@max-[600px]:hidden";
const UNDER_TITLE = "max-[760px]:row-start-2";
const NARROW_LINK = "max-[760px]:absolute max-[760px]:inset-0 max-[760px]:text-[0px]";
const DASH = "–";
const NEEDS_YOU = new Set<State>(["stale", "question", "paused", "ready_to_merge"]);
const DAY_NAME = new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric" });

function clock(ms: number, now: number): string {
  const today = localMidnight(now);
  if (ms >= today) return formatClock(ms);
  return ms >= localMidnight(today - 1) ? `yest ${formatClock(ms)}` : formatStamp(ms);
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

function StateGlyph({ row }: { row: RunRow }) {
  const [shown, setShown] = useState(false);
  return (
    <span
      tabIndex={0}
      onPointerEnter={() => setShown(true)}
      onPointerLeave={() => setShown(false)}
      onFocus={() => setShown(true)}
      onBlur={() => setShown(false)}
      onKeyDown={(event) => event.key === "Escape" && setShown(false)}
      className={`relative inline-flex size-4 items-center justify-center focus-visible:outline-2 focus-visible:outline-focus ${shown ? "z-30" : "z-[1]"}`}
    >
      <StateDot state={row.state} hideWord label={row.stage} />
      {shown && (
        <span role="tooltip" className="absolute top-full left-0 mt-1.5 w-max max-w-[min(320px,calc(100vw-32px))] border border-rule bg-paper px-3 py-2 font-sans text-[13px] leading-[1.45] text-ink shadow-[var(--card-shadow)]">
          {row.stage}
        </span>
      )}
    </span>
  );
}

function LoopGlyph() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
      <path d="M3.2 8a4.8 4.8 0 1 0 1.4-3.4" />
      <path d="M3.2 2.6v2.6h2.6" />
    </svg>
  );
}

/** Open run, Copy link, and Revert… for a merged run of a project that
 *  reports `reverts`. */
function useRunRowMenu(row: RunRow) {
  const status = useConsolePeers().hosts.find((host) => host.base === row.base)?.status;
  const revertable = row.state === "merged" && status?.reverts === true;
  const revert = useRevertFlow(revertable ? { base: row.base, project: row.project, ticket: row.ticket, run: row.id, sha: row.merge_sha, reverted: row.reverted, off: status?.actions === true ? null : ACTIONS_OFF } : null);
  const place = { project: row.project, run: row.id };
  const items = usePlaceItems(place, place);
  return { items: revert.item == null ? items : [...items, revert.item], layer: revert.layer };
}

function RunTableRow({ row, now, colour, grouped }: { row: RunRow; now: number; colour: string | undefined; grouped: boolean }) {
  const menu = useRunRowMenu(row);
  const start = clock(row.started_ms, now);
  const shownStart = grouped ? formatClock(row.started_ms) : start;
  const need = NEEDS_YOU.has(row.state);
  return (
    <li
      data-row
      data-run={row.id}
      data-need={need ? "" : undefined}
      className={`${GRID} ${grouped ? DAY_COLUMNS : DATED_COLUMNS} group relative min-h-8 border-b border-rule-2 pl-2.5 pr-11 text-[14px] text-ink transition-colors duration-(--motion-colour) hover:bg-hover max-[760px]:py-1.5 ${need ? "bg-(image:--need-tint) shadow-[inset_2px_0_0_var(--need)]" : ""}`}
    >
      <span className="inline-flex max-[760px]:row-span-2"><StateGlyph row={row} /></span>
      <RunRowLink row={row} aria-label={`Run ${row.id}, ${row.ticket} ${plainTitle(row.title)}, ${row.project}, ${row.stage}, started ${start}`} className={`${MONO} ${NARROW_LINK} no-underline after:absolute after:inset-0 focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-focus`}>
        {row.id}
      </RunRowLink>
      <span className={`${MONO} ${UNDER_TITLE} truncate max-[760px]:col-start-2`}>
        <Link
          to="/tickets/$project/$key"
          params={{ project: row.project, key: row.ticket }}
          search={keepScope}
          className="relative no-underline hover:text-ink hover:underline"
        >
          {row.ticket}
        </Link>
      </span>
      <span data-title className="flex min-w-0 items-baseline gap-2 max-[760px]:col-span-3 max-[760px]:col-start-2 max-[760px]:mb-1">
        <span className="truncate font-read text-[14px] font-medium leading-[1.3]"><TitleText>{row.title}</TitleText></span>
        {row.resumed_as != null && <span className="shrink-0 font-mono text-[11.5px] text-ink-3"><ResumedAs row={row} /></span>}
      </span>
      <span className={`${WIDE} flex min-w-0 items-center gap-2 font-sans text-[13px] text-ink-2 min-[760px]:@max-[780px]:hidden`}>
        <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full" style={{ background: colour ?? "var(--ink-3)" }} />
        <span className="truncate">{row.project}</span>
      </span>
      <span data-rounds className={`${MONO} ${ROOMY} inline-flex items-center gap-1`}>
        {row.rounds > 0 ? <><LoopGlyph />{row.rounds}</> : DASH}
      </span>
      <span data-pr className={`${MONO} ${ROOMY}`}>{prNumber(row.pr_url)}</span>
      <span className={`${MONO} ${UNDER_TITLE} whitespace-nowrap max-[760px]:col-start-3 max-[760px]:before:content-['·_']`}>{shownStart}</span>
      <span className={`${MONO} whitespace-nowrap text-right max-[760px]:col-start-5 max-[760px]:row-span-2 max-[760px]:row-start-1`}>{formatSettled(row.elapsed_ms)}</span>
      <MoreMenu label={`More for run ${row.id}`} items={menu.items} className={`${ROW_MENU} absolute top-0 right-1.5 z-[2] flex h-8 items-center`} />
      <div className="col-span-full pb-1.5 empty:hidden">{menu.layer}</div>
    </li>
  );
}

function daysOf(rows: RunRow[], now: number) {
  const days: { key: string; label: string; rows: RunRow[] }[] = [];
  for (const row of rows) {
    const date = new Date(row.started_ms);
    const key = `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
    const last = days.at(-1);
    if (last?.key === key) {
      last.rows.push(row);
      continue;
    }
    const back = daysAgo(row.started_ms, now);
    days.push({ key, label: back === 0 ? "Today" : back === 1 ? "Yesterday" : DAY_NAME.format(row.started_ms), rows: [row] });
  }
  return days;
}

/** `grouped` lists the rows under a heading for each local day they start
 *  on, in their order; `dock` sits above the rows. */
export function RunTable({ rows, now, empty, label = "Runs, newest first", dock, grouped = false }: {
  rows: RunRow[]; now: number; empty: string; label?: string; dock?: ReactNode; grouped?: boolean;
}) {
  const colours = projectColours(useConsolePeers().hosts);
  const id = useId();
  const list = (each: RunRow[]) => each.map((row) => <RunTableRow key={`${row.base} ${row.id}`} row={row} now={now} colour={colours.get(row.project)} grouped={grouped} />);
  return (
    <div data-run-table className="@container">
      {dock}
      <div data-run-list className="flex flex-col gap-5">
        {grouped ? (
          daysOf(rows, now).map((day) => (
            <section key={day.key} aria-labelledby={`${id}-${day.key}`}>
              <h2 data-day className="m-0 flex h-8 items-center gap-2 rounded-card bg-hover px-2.5 shadow-[inset_0_0_0_1px_var(--hair)] font-sans text-[13px] font-semibold text-ink">
                <span id={`${id}-${day.key}`}>{day.label}</span> <span className="font-mono text-[12px] font-normal tabular-nums text-ink-3">{day.rows.length}</span>
              </h2>
              <ol aria-labelledby={`${id}-${day.key}`} className="m-0 list-none p-0">{list(day.rows)}</ol>
            </section>
          ))
        ) : (
          rows.length > 0 && <ol aria-label={label} className="m-0 list-none p-0">{list(rows)}</ol>
        )}
        {rows.length === 0 && <p className="m-0 px-2.5 py-[18px] font-mono text-[13px] leading-[1.4] text-ink-3">{empty}</p>}
      </div>
    </div>
  );
}
