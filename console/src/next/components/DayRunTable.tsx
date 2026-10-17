import { formatClock, formatSettled } from "../../lib/format";
import { localMidnight } from "../../lib/ledger";
import { failureWord, firstLine } from "../history";
import type { DayGroup, DayRun } from "../runDays";
import { STATES } from "../state";
import { prNumber, RunRowLink } from "./RunTable";
import { StateDot, TONE_TEXT } from "./StateDot";

const GRID = "grid grid-cols-[56px_96px_minmax(0,2.6fr)_96px_168px_64px_minmax(170px,1.2fr)_60px] items-start gap-3";
const MONO = "font-mono text-[12px] leading-[1.4]";
const weekday = new Intl.DateTimeFormat("en-GB", { weekday: "short" });

function reason(row: DayRun): string {
  if (row.state === "merged") {
    const pr = prNumber(row.pr_url);
    return pr === "–" ? "" : `PR ${pr}`;
  }
  if (row.state === "failed") return failureWord(row.failure_kind).replace(/^failed(?: · )?/, "").trim();
  return row.state === "abandoned" ? firstLine(row.outcome_reason) : "";
}

function Took({ row }: { row: DayRun }) {
  const { working_ms: work, estimate_ms: estimate } = row;
  if (work == null) return <span className={`${MONO} text-ink-3`}>–</span>;
  const over = estimate == null ? 0 : Math.floor((work - estimate) / 60_000);
  const fill = estimate === 0 ? (work > 0 ? 100 : 0) : Math.min(100, Math.round(work / (1.5 * (estimate ?? 1)) * 100));
  const tone = estimate != null && work > estimate ? "bg-need" : row.state === "merged" ? "bg-live" : "bg-ink-3";
  return (
    <span className={`${MONO} text-ink-3 tabular-nums`}>
      <span className="font-medium text-ink-2">{formatSettled(work)}</span>
      {estimate != null && <>
        {" "}<span>/ {formatSettled(estimate)}</span>
        {over >= 1 && <span className="text-need"> +{over}m</span>}
        <span data-work-bar aria-hidden="true" className="relative mt-2 block h-1 bg-rule-2">
          <span data-work-fill className={`block h-full ${tone}`} style={{ width: `${fill}%` }} />
          <span className="absolute -top-0.5 h-2 w-px bg-ink-3" style={{ left: `${100 / 3 * 2}%` }} />
        </span>
      </>}
    </span>
  );
}

function DayRow({ row, since }: { row: DayRun; since: number }) {
  const ended = row.ended_ms == null ? "–" : `${localMidnight(row.ended_ms) > since ? `${weekday.format(row.ended_ms)} ` : ""}${formatClock(row.ended_ms)}`;
  const took = row.working_ms == null ? "" : `, took ${formatSettled(row.working_ms)}${row.estimate_ms == null ? "" : ` against ${formatSettled(row.estimate_ms)}`}`;
  const detail = reason(row);
  return (
    <li data-run={row.id} className={`${GRID} relative min-h-[52px] border-b border-rule-2 p-3 transition-colors duration-(--motion-colour) hover:bg-paper-2`}>
      <RunRowLink row={row} aria-label={`Run ${row.id}, ${row.ticket} ${row.title}, ${row.project}, ${STATES[row.state].word}${took}, ended ${ended}`} className={`${MONO} text-ink-2 no-underline after:absolute after:inset-0 focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-act`}>
        {row.id}
      </RunRowLink>
      <span className="font-mono text-[11.5px] text-ink-3 [overflow-wrap:anywhere]">{row.ticket}</span>
      <span className="min-w-0 font-sans text-[14px] font-semibold leading-[1.35] text-ink [overflow-wrap:anywhere]">{row.title}</span>
      <span className={`${MONO} text-ink-3 [overflow-wrap:anywhere]`}>{row.project}</span>
      <span>
        <span className="flex items-center gap-1.5"><StateDot state={row.state} hideWord /><span className={`${MONO} font-medium ${TONE_TEXT[STATES[row.state].tone]}`}>{STATES[row.state].word}</span></span>
        {detail && <span className="mt-1 block font-mono text-[11.5px] leading-[1.4] text-ink-3 [overflow-wrap:anywhere]">{detail}</span>}
      </span>
      <span className={`${MONO} text-ink-3`}>{row.rounds || "–"}</span>
      <Took row={row} />
      <span className={`${MONO} text-ink-3 text-right`}>{ended}</span>
    </li>
  );
}

export function DayRunTable({ day, label }: { day: DayGroup; label: string }) {
  return (
    <div data-day-run-table className="overflow-x-auto">
      <div className="min-w-[1000px]">
        <div aria-hidden="true" className={`${GRID} border-b border-rule px-3 pb-2.5 pt-3 font-mono text-[11px] uppercase leading-none tracking-[0.12em] text-ink-3`}>
          {["Run", "Ticket", "Title", "Project", "Outcome", "Rounds", "Took · estimate", "Ended"].map(word => <span key={word} className={word === "Ended" ? "text-right" : undefined}>{word}</span>)}
        </div>
        <ol aria-label={`Runs on ${label}, latest first`} className="m-0 list-none p-0">
          {day.rows.map(row => <DayRow key={`${row.base} ${row.id}`} row={row} since={day.since} />)}
        </ol>
      </div>
    </div>
  );
}
