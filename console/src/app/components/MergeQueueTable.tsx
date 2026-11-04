import { useId, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import type { MergeQueue, MergeQueueEntry, MergeQueueLeft, MergeQueuePull, MergeQueueRun } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { checkLook, configurationWords, entryLine, isBuilding, mergesIn, runWords, splitChecks, type CheckSplit } from "../mergeQueue";
import { keepScope } from "../scope";
import { useTableWidth } from "../viewport";
import { LaneHead } from "./LaneHead";
import { JumpToFront, RemoveFromQueue, type QueueDaemon } from "./QueueActions";
import { StateDot, TONE_TEXT } from "./StateDot";
import { TitleText } from "./TitleText";

const GRID = "grid grid-cols-[44px_64px_minmax(0,1fr)_150px_104px_64px_56px] items-start gap-x-3";
const ACTION_GRID = "grid grid-cols-[44px_64px_minmax(0,1fr)_150px_104px_64px_56px_124px] items-start gap-x-3";
const gridFor = (daemon: QueueDaemon) => (daemon.actions ? ACTION_GRID : GRID);
const NARROW_GRID = "grid grid-cols-[28px_minmax(0,1fr)_auto] items-start gap-x-3";
const MONO = "font-mono text-[12px] leading-[1.4] text-ink-3";
const DASH = "–";
const HEADS = ["#", "PR", "Title", "State", "Checks", "Merges", "Added"];
const DOT = <span className="text-ink-3">{" · "}</span>;
const ENTRY_PAD = "pr-5 pl-[calc(12px+44px+12px+64px+12px)] max-[1099px]:pl-[calc(12px+28px+12px)] max-[720px]:px-3";

function PrNumber({ pr }: { pr: MergeQueuePull }) {
  return (
    <a href={pr.url} target="_blank" rel="noreferrer" className="whitespace-nowrap border-b border-rule text-ink-2 hover:border-ink hover:text-ink">
      #{pr.number}
    </a>
  );
}

/** The run's ticket key, linking to its ticket page, and its phase; a hand pull request's author. */
function Carrier({ project, run, author, round = false }: { project: string; run: MergeQueueRun | null; author: string | null; round?: boolean }) {
  if (run == null) return <>{author ?? DASH}</>;
  return (
    <>
      <Link to="/tickets/$project/$key" params={{ project, key: run.ticket }} search={keepScope} className="text-ink-2 underline underline-offset-2 hover:text-ink">
        {run.ticket}
      </Link>
      {DOT}
      {runWords(run, round)}
    </>
  );
}

function EntryState({ entry }: { entry: MergeQueueEntry }) {
  const building = isBuilding(entry);
  const tone = building ? "wait" : "ink-2";
  return (
    <span data-entry-state className="inline-flex items-start gap-1.5">
      <StateDot state={building ? "on_ci" : "queued"} tone={tone} hideWord />
      <span className={`font-sans text-[13px] font-medium leading-[1.3] ${TONE_TEXT[tone]}`}>{building ? "checks running" : "queued"}</span>
    </span>
  );
}

function ChecksDone({ entry }: { entry: MergeQueueEntry }) {
  if (entry.checks.total === 0) return <>{DASH}</>;
  return (
    <span className="whitespace-nowrap">
      <b className="font-semibold text-ink">{entry.checks.done}</b>/{entry.checks.total} done
    </span>
  );
}

function CheckColumn({ title, part, now }: { title: string; part: CheckSplit; now: number }) {
  return (
    <section aria-label={title} className="min-w-0">
      <h3 className="flex items-baseline gap-2 border-b border-rule-2 pb-2 font-mono text-[11px] font-medium text-ink-3">
        <span className="uppercase tracking-[0.12em]">{title}</span>
        <span data-check-count className="text-[12px]">{part.done} of {part.checks.length} done</span>
      </h3>
      <ul className="m-0 list-none p-0">
        {part.checks.map((check, index) => {
          const look = checkLook(check, now);
          return (
            <li key={`${check.name} ${index}`} data-check className="grid grid-cols-[16px_minmax(0,1fr)_auto] items-start gap-x-2 border-b border-dashed border-rule-2 py-2 font-mono text-[13px] leading-[1.35] last:border-b-0">
              <StateDot state={look.state} tone={look.tone} label={look.words || check.status} hideWord />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {check.url ? (
                  <a href={check.url} target="_blank" rel="noreferrer" className="text-ink hover:underline">{check.name}</a>
                ) : (
                  <span className="text-ink">{check.name}</span>
                )}
                {check.workflow && <span className="ml-2 text-ink-3">{check.workflow}</span>}
              </span>
              <span className={`whitespace-nowrap text-right ${look.state === "ready_to_merge" ? "text-ink-3" : TONE_TEXT[look.tone]}`}>{look.words}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function EntryDetail({ entry, now, id, daemon, pad, links }: { entry: MergeQueueEntry; now: number; id: string; daemon: QueueDaemon; pad: string; links?: ReactNode }) {
  const { required, other } = splitChecks(entry.checks.runs);
  const line = entryLine(entry, now);
  return (
    <div id={id} data-entry-detail className={`flex flex-col gap-4 border-t border-dashed border-rule-2 py-4 ${pad}`}>
      {line && <p className={MONO}>{line}</p>}
      {entry.checks.runs.length > 0 && (
        <div className="grid grid-cols-2 gap-x-10 gap-y-5 max-[720px]:grid-cols-1">
          <CheckColumn title="Required" part={required} now={now} />
          <CheckColumn title="Other" part={other} now={now} />
        </div>
      )}
      <div className="flex flex-wrap gap-2.5">
        <a href={entry.pr.url} target="_blank" rel="noreferrer" className="btn" data-variant="quiet">Open on GitHub</a>
        {links}
        {daemon.actions && entry.run == null && <RemoveFromQueue entry={entry} daemon={daemon} />}
      </div>
    </div>
  );
}

function EntryRow({ queue, entry, project, wide, daemon }: { queue: MergeQueue; entry: MergeQueueEntry; project: string; wide: boolean; daemon: QueueDaemon }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const title = entry.pr.title ?? `#${entry.pr.number}`;
  const added = entry.enqueued_ms == null ? DASH : formatClock(entry.enqueued_ms);
  const toggle = (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={open ? detailId : undefined}
      aria-label={`Position ${entry.position}, #${entry.pr.number} checks`}
      onClick={() => setOpen(!open)}
      className="flex min-h-11 items-start gap-2 pt-[3px] font-mono text-[13px] text-ink-2 hover:text-ink"
    >
      <svg viewBox="0 0 10 10" width={10} height={10} fill="none" stroke="currentColor" strokeWidth={1.4} aria-hidden="true" className={`mt-[3px] shrink-0 ${open ? "rotate-90" : ""}`}>
        <path d="M3.5 1.5 7 5 3.5 8.5" />
      </svg>
      {entry.position}
    </button>
  );
  const titleText = <span data-entry-title className="font-read text-[15px] font-medium leading-[1.35] text-ink"><TitleText>{title}</TitleText></span>;
  const carrier = <Carrier project={project} run={entry.run} author={entry.pr.author} />;
  const jump = daemon.actions && entry.position > 1 ? <JumpToFront queue={queue} entry={entry} daemon={daemon} /> : null;
  return (
    <li data-entry={entry.position} data-open={open || undefined} className="border-b border-rule-2 last:border-b-0 data-open:bg-row-selected data-open:shadow-[inset_2px_0_0_var(--act)]">
      {wide ? (
        <div data-row className={`${gridFor(daemon)} px-3 py-3`}>
          {toggle}
          <span className="pt-[3px] font-mono text-[13px]"><PrNumber pr={entry.pr} /></span>
          <span className="flex min-w-0 flex-col gap-1">
            {titleText}
            <span data-entry-sub className={MONO}>{carrier}</span>
          </span>
          <EntryState entry={entry} />
          <span className={`${MONO} pt-[2px]`}><ChecksDone entry={entry} /></span>
          <span className={`${MONO} pt-[2px] text-ink-2`}>{mergesIn(entry.estimated_s)}</span>
          <span className={`${MONO} pt-[2px]`}>{added}</span>
          {daemon.actions && <span className="justify-self-end">{jump}</span>}
        </div>
      ) : (
        <div data-row className={`${NARROW_GRID} px-3 py-3`}>
          {toggle}
          <span className="flex min-w-0 flex-col gap-1.5">
            {titleText}
            <span data-entry-sub className={`${MONO} [overflow-wrap:anywhere]`}>
              <PrNumber pr={entry.pr} />
              {DOT}
              {carrier}
            </span>
            <span className={`${MONO} [overflow-wrap:anywhere]`}>
              {entry.checks.total > 0 && <><ChecksDone entry={entry} />{DOT}</>}
              <span className="whitespace-nowrap">merges {mergesIn(entry.estimated_s)}</span>
              {DOT}
              <span className="whitespace-nowrap">added {added}</span>
            </span>
            {jump && <span className="pt-1">{jump}</span>}
          </span>
          <EntryState entry={entry} />
        </div>
      )}
      {open && <EntryDetail entry={entry} now={queue.now} id={detailId} daemon={daemon} pad={ENTRY_PAD} />}
    </li>
  );
}

/** One queue's settings tab over its entries, a row each that opens onto its checks. */
export function QueueTable({ queue, project, daemon }: { queue: MergeQueue; project: string; daemon: QueueDaemon }) {
  const wide = useTableWidth();
  return (
    <div data-queue-table className="min-w-0">
      <LaneHead name={queue.branch} held={false} meta={configurationWords(queue.configuration)} />
      <div className="border border-rule bg-paper">
        {wide && (
          <div data-queue-heads className={`${gridFor(daemon)} border-b border-rule px-3 pt-3 pb-2.5 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3`}>
            {HEADS.map((head) => <span key={head}>{head}</span>)}
          </div>
        )}
        <ol aria-label={`${project} merge queue`} className="m-0 list-none p-0">
          {queue.entries.map((entry) => (
            <EntryRow key={entry.pr.number} queue={queue} entry={entry} project={project} wide={wide} daemon={daemon} />
          ))}
        </ol>
        {queue.entries.length === 0 && <p className="m-0 px-3 py-[18px] font-mono text-[13px] leading-[1.4] text-ink-3">The queue is empty.</p>}
      </div>
    </div>
  );
}

function LeftRow({ row, project }: { row: MergeQueueLeft; project: string }) {
  const merged = row.outcome === "merged";
  const tone = merged ? "live" : "bad";
  const words = merged ? "merged" : row.reason ? `removed: ${row.reason}` : "removed";
  return (
    <li data-left className="grid grid-cols-[64px_minmax(0,1fr)_auto] items-start gap-x-4 border-b border-rule-2 px-3 py-3 last:border-b-0 max-[720px]:grid-cols-[minmax(0,1fr)_auto]">
      <span className="pt-[3px] font-mono text-[13px] max-[720px]:hidden"><PrNumber pr={row.pr} /></span>
      <span className="flex min-w-0 flex-col gap-1">
        <span className="font-read text-[15px] font-medium leading-[1.35] text-ink-2"><TitleText>{row.pr.title ?? `#${row.pr.number}`}</TitleText></span>
        <span className={`${MONO} [overflow-wrap:anywhere]`}>
          <span className="min-[721px]:hidden"><PrNumber pr={row.pr} />{DOT}</span>
          <Carrier project={project} run={row.run} author={row.pr.author} round />
        </span>
      </span>
      <span className="flex flex-col items-end gap-1 text-right">
        <span data-left-outcome className={`inline-flex items-start gap-1.5 font-sans text-[13px] font-medium leading-[1.3] ${TONE_TEXT[tone]}`}>
          <StateDot state={merged ? "merged" : "failed"} tone={tone} label={merged ? "merged" : "removed"} hideWord />
          {words}
        </span>
        <span data-left-at className={MONO}>{formatClock(row.at_ms)}</span>
      </span>
    </li>
  );
}

/** What left the queue in the answer's window, newest first; nothing when nothing left. */
export function LeftTheQueue({ left, project }: { left: MergeQueueLeft[]; project: string }) {
  if (left.length === 0) return null;
  const rows = [...left].sort((a, b) => b.at_ms - a.at_ms);
  return (
    <section aria-label={`${project} left the queue`} className="flex flex-col gap-3">
      <h2 className="flex items-baseline gap-3 font-sans text-[16px] font-semibold text-ink">
        Left the queue <span className="font-mono text-[12px] font-normal text-ink-3">last 2h</span>
      </h2>
      <ol className="m-0 list-none border border-rule bg-paper p-0">
        {rows.map((row) => <LeftRow key={`${row.pr.number} ${row.at_ms}`} row={row} project={project} />)}
      </ol>
    </section>
  );
}
