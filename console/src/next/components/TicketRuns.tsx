import type { StatusRun, TicketRun } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { phaseLabel } from "../../lib/runs";
import { dayLabel, daysAgo, prLabel } from "../../lib/shipped";
import { classicHref } from "../../lib/view";
import { STATES, stateOf } from "../state";
import { SECTION_LABEL } from "./Criteria";
import { StateDot } from "./StateDot";

const DASH = "–";
const HEAD = "border-b border-rule px-2.5 pb-2 pt-2.5 text-left font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3";
const CELL = "whitespace-nowrap border-b border-rule-2 p-2.5 align-top text-ink-2";
const LINK = "text-ink underline-offset-2 hover:underline";

/** "today 16:36", "yesterday 15:10", else "Mon Oct 12 15:10", by `now`'s day. */
export function startedLabel(ms: number, now: number): string {
  const days = daysAgo(ms, now);
  const day = days === 0 ? "today" : days === 1 ? "yesterday" : dayLabel(ms, days);
  return `${day} ${formatClock(ms)}`;
}

function RunRow({ run, live, staleMs, base, now }: { run: TicketRun; live: StatusRun | undefined; staleMs: number; base: string; now: number }) {
  const state = stateOf({ run: { ...live, phase: run.phase, outcome: run.outcome }, staleMs });
  const word = state === "running" ? phaseLabel(run.phase, run.pr_url) : STATES[state].word;
  const rounds = run.outcome == null && live != null ? `${live.round + 1} of ${live.max_rounds}` : run.rounds > 0 ? String(run.rounds) : DASH;
  return (
    <tr data-run={run.run}>
      <td className={CELL}>
        <a href={classicHref(`#run=${run.run}&daemon=${encodeURIComponent(base)}`)} className={LINK}>
          run {run.run}
        </a>
      </td>
      <td className={CELL}>
        <span className="inline-flex items-center gap-[7px]">
          <StateDot state={state} hideWord />
          <span>{word}</span>
        </span>
      </td>
      <td className={CELL}>{rounds}</td>
      <td className={CELL}>
        {run.pr_url == null ? DASH : <a href={run.pr_url} className={LINK}>{prLabel(run.pr_url)}</a>}
      </td>
      <td className={`${CELL} text-right text-ink-3`}>{startedLabel(run.started_ms, now)}</td>
    </tr>
  );
}

/** Every run of the ticket, newest first; a live run's rounds come from `/status`. */
export function TicketRuns({ ticketKey, runs, liveRuns, staleMs, base, now }: {
  ticketKey: string;
  runs: TicketRun[];
  liveRuns: StatusRun[];
  staleMs: number;
  base: string;
  now: number;
}) {
  const newest = [...runs].sort((a, b) => b.started_ms - a.started_ms || b.run - a.run);
  return (
    <section aria-labelledby="ticket-runs" className="mt-[26px]">
      <div id="ticket-runs" className={SECTION_LABEL}>
        <span>
          Runs <span className="text-ink">{runs.length}</span>
        </span>
      </div>
      {newest.length === 0 ? (
        <p className="m-0 mt-3 font-mono text-[12.5px] leading-[1.4] text-ink-3">No run has claimed {ticketKey} yet.</p>
      ) : (
        <div data-ticket-runs className="mt-2 overflow-x-auto">
          <table className="w-full border-collapse font-mono text-[12.5px] leading-[1.4]">
            <thead>
              <tr>
                <th scope="col" className={HEAD}>Run</th>
                <th scope="col" className={HEAD}>Outcome</th>
                <th scope="col" className={HEAD}>Rounds</th>
                <th scope="col" className={HEAD}>PR</th>
                <th scope="col" className={`${HEAD} text-right`}>When</th>
              </tr>
            </thead>
            <tbody>
              {newest.map((run) => (
                <RunRow key={run.run} run={run} live={liveRuns.find((each) => each.id === run.run)} staleMs={staleMs} base={base} now={now} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
