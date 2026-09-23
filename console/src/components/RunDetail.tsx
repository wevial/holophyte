import { TicketLink } from "./TicketLink";
import { useState } from "react";
import { useRunDetail } from "../hooks/useRunDetail";
import { useRunFiles, type RunFilesState } from "../hooks/useRunFiles";
import { useRunLedger } from "../hooks/useRunLedger";
import { useRunTurns, type RunTurnsBody } from "../hooks/useRunTurns";
import { FATE_LABEL, findingsHistory, openFindings, severityCounts, type Fate, type RoundHistory } from "../lib/findings";
import { formatClock, formatSettled, formatSpan } from "../lib/format";
import type { LedgerRow } from "../lib/ledger";
import type { Fetch } from "../lib/poll";
import { phaseLabel, roundLabel } from "../lib/runs";
import { agentMs, verifyMs } from "../lib/runs";
import { buildTimeline } from "../lib/timeline";
import type { Round, RunDetailBody } from "../lib/types";
import { ActionButton } from "./ActionButton";
import { FilesTouched } from "./FilesTouched";
import { FindingCard } from "./FindingCard";
import { OperatorNoteCard } from "./OperatorNoteCard";
import { InstructionCard } from "./InstructionCard";
import { RoundTimeline } from "./RoundTimeline";
import { RunTurns } from "./RunTurns";
import { RunLog } from "./RunLog";
import { ReasonAction } from "./ReasonAction";
import type { RowDaemon } from "./RowActions";
import { PrLink, Sha } from "./ShippedTable";

/** Count independent reviews against their cap; other rounds have no review budget. */
export function roundLine(body: RunDetailBody): string {
  const reviewer = body.run.reviewer_model ?? body.rounds[0]?.reviewer_model;
  const reviews = body.rounds.filter(
    round => round.reviewer_model === reviewer && round.verdict !== "error").length;
  const other = body.rounds.length - reviews;
  const max = body.run.max_rounds ?? reviews;
  return `Review ${reviews} of ${max}${other ? ` · ${other} other rounds` : ""} · ${phaseLabel(body.run.phase, body.run.pr_url)}`;
}

const SEATS = [["implement", "Implementer"], ["review", "Reviewer"]] as const;

/** "Implementer claude opus · Reviewer codex astra": the models the run's
 *  turns used in each seat, a seat it never used left out. */
function seatLine(turns: RunTurnsBody["turns"]): string {
  return SEATS.flatMap(([role, seat]) => {
    const labels = new Set(turns.filter((turn) => turn.role === role).map((turn) => turn.label ?? "label unknown"));
    return labels.size === 0 ? [] : [`${seat} ${[...labels].join(", ")}`];
  }).join(" · ");
}

/** The expanded run's card: header line, round timeline, the newest
 *  round's open findings and the run log from `/runs/N`, the files touched
 *  from `/runs/N/files`, and the turns from `/runs/N/turns`, whose models
 *  the header names per seat; each read on expand and again each poll. Given
 *  its `daemon`, a live run has Abort in the footer, Abort and close too
 *  when it has a pull request, and Pause when it has no `stopRequested`. */
export function RunDetail({
  base,
  id,
  now,
  sinceMs = 0,
  polls,
  deps,
  daemon,
  stopRequested,
}: {
  base: string;
  id: number;
  /** The daemon's clock at the last poll, so the timeline agrees with the row's time box. */
  now: number;
  /** Local milliseconds since that poll; the run log's summary ages by it. */
  sinceMs?: number;
  polls: number;
  deps?: { fetch: Fetch };
  daemon?: RowDaemon;
  /** The live run's pending stop request from `/status`, if any. */
  stopRequested?: string | null;
}) {
  const { detail, error, loading } = useRunDetail(base, id, polls, deps);
  const files = useRunFiles(base, id, polls, deps);
  const turns = useRunTurns(base, id, polls, deps);
  // The ledger is only read for a finished run's findings history; a live
  // run fetches none.
  const ledger = useRunLedger(base, detail?.run.ended_ms != null ? id : null, polls, deps);
  return (
    <div data-detail className="pr-4 pb-[14px] pl-[44px]">
      {loading && <p className="text-[12px] text-muted">loading…</p>}
      {error && (
        <p role="alert" data-detail-error className="text-[12px] font-semibold text-bad">
          {error}
        </p>
      )}
      {detail && <Card body={detail} files={files} ledger={ledger} seats={seatLine(turns.body?.turns ?? [])}
        now={now} sinceMs={sinceMs} daemon={daemon} pauseDaemon={stopRequested ? undefined : daemon} />}
      {detail && <RunTurns key={`${base}/${id}`} base={base} id={id} turns={turns} deps={deps} />}
    </div>
  );
}

function Card({
  body,
  files,
  ledger,
  seats,
  now,
  sinceMs,
  daemon,
  pauseDaemon,
}: {
  body: RunDetailBody;
  files: RunFilesState;
  ledger: LedgerRow[];
  seats: string;
  now: number;
  sinceMs: number;
  daemon?: RowDaemon;
  pauseDaemon?: RowDaemon;
}) {
  const { run } = body;
  const rounds = [...body.rounds].sort((a, b) => a.started_ms - b.started_ms);
  // The card's clock: the daemon's at the last poll plus the local drift
  // since. A finished run's figures measure against its end instead, so a
  // live run keeps counting between polls and a finished one stays put —
  // and reads at settled granularity, its seconds done counting too.
  const tickingNow = now + sinceMs;
  const work = agentMs(run, run.ended_ms == null ? sinceMs : 0);
  const verify = verifyMs(run, run.ended_ms == null ? sinceMs : 0);
  const remaining = work == null || run.time_box_ms == null ? null : run.time_box_ms - work;
  const over = remaining != null && remaining < 0;
  const finished = run.ended_ms != null;
  const boxFigure = finished ? formatSettled : formatSpan;
  const findings = openFindings(rounds);
  const counts = severityCounts(findings);
  return (
    <article aria-label={`run ${run.id}`} className="rounded-[10px] border border-line bg-card px-4 py-3">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <TicketLink ticket={run.ticket} ticket_url={run.ticket_url} />
        <span className="text-[13px] font-semibold text-ink">{roundLine(body)}</span>
        {seats && <span data-seats className="text-[12px] text-muted">{seats}</span>}
        <span data-started className="text-[12px] text-muted">
          started {formatClock(run.started_ms)}
          {run.host ? ` · ${run.host}` : ""}
        </span>
        {run.merge_sha && <Sha row={{ merge_sha: run.merge_sha, commit_url: run.commit_url }} />}
        <PrLink url={run.pr_url} />
        <span
          data-box={over ? "over" : "left"}
          className={`ml-auto font-mono text-[12px] ${over ? "font-semibold text-bad" : "text-muted"}`}
        >
          {run.time_box_ms == null ? "working box unknown" : remaining == null ? "working n/a" : over ? `${boxFigure(-remaining)} over the working box` : `${boxFigure(remaining)} left in working box`}
          {" · wall "}{boxFigure((run.ended_ms ?? tickingNow) - run.started_ms)}
        </span>
        <span data-clocks className="font-mono text-[12px] text-muted">
          agent {work == null ? "n/a" : boxFigure(work)} · verify {verify == null ? "n/a" : boxFigure(verify)}
        </span>
      </header>
      {run.approved_at != null && (
        <p className="text-[12px] text-muted">
          approved by {run.approved_by} at {new Date(run.approved_at).toISOString()}
        </p>
      )}
      <div className="mt-3 grid grid-cols-[1fr_280px] gap-7">
        <div className="min-w-0">
          <RoundTimeline
            segments={buildTimeline({ ...run, rounds, events: body.events }, tickingNow)}
            run={run}
            now={tickingNow}
          />
          {rounds.flatMap((round, index) => (round.operator_notes ?? []).map((note) => (
            <OperatorNoteCard key={note.event_id} note={note} ordinal={index + 1}
              started={body.events.find((event) => event.kind === "operator_note_consumed" &&
                event.summary === `operator_note event ${note.event_id} drove round ${round.round}`)?.at ?? round.started_ms} />
          )))}
          {rounds.some((round) => (round.instructions ?? []).length > 0) && (
            <section className="mt-4">
              <h3 className="text-sm font-semibold">Instructions</h3>
              <ul className="mt-2 flex flex-col gap-2">
                {rounds.flatMap((round) => (round.instructions ?? []).map((instruction, index) => (
                  <InstructionCard key={`${round.round}-${index}`} instruction={instruction} />
                )))}
              </ul>
            </section>
          )}
          {finished ? (
            <FindingsSection rounds={rounds} ledger={ledger} />
          ) : (
            <>
              <div className="mt-4 flex items-baseline gap-3">
                <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">Open findings</span>
                <span data-severity-counts className="font-mono text-[12px] text-muted">
                  {counts.must} must · {counts.should} should
                </span>
              </div>
              {findings.length === 0 && (
                <p className="mt-2 text-[13px] text-muted">
                  {rounds.length === 0 ? "No review round yet" : "No open findings"}
                </p>
              )}
              {rounds.length > 0 && <FindingsSection rounds={rounds} ledger={ledger} showHeading={false} />}
            </>
          )}
          {(body.findings ?? []).length > 0 && (
            <ul aria-label="Advisory findings" className="mt-3 flex flex-col gap-2">
              {body.findings!.map((finding, index) => (
                <li key={index} className="rounded border border-line bg-card px-3 py-2 text-[13px] text-muted">
                  <span className="mr-2 font-semibold">advisory</span>
                  {finding.message}
                </li>
              ))}
            </ul>
          )}
        </div>
        <FilesTouched files={files.files} error={files.error} status={files.status} pending={files.pending} loading={files.loading} />
      </div>
      <footer className="mt-3 flex gap-2">
        {daemon && !finished && <ReasonAction daemon={daemon} route="/actions/abort" body={{ run: run.id, close: false }} label="Abort" />}
        {daemon && !finished && run.pr_url && <ReasonAction daemon={daemon} route="/actions/abort" body={{ run: run.id, close: true }} label="Abort and close" />}
        <ActionButton>Requeue ticket</ActionButton>
        {pauseDaemon && !finished && <ReasonAction daemon={pauseDaemon} route="/actions/pause" body={{ run: run.id }} label="Pause" />}
      </footer>
      <RunLog events={body.events} rounds={rounds} now={tickingNow} />
    </article>
  );
}

const FATE_ORDER: Fate[] = ["fixed", "declined", "follow_up", "open"];

/** A fold header's tail: "fixed" when every finding shares the one fate,
 *  else each fate with its count ("2 fixed · 1 open"); "" for no findings. */
function fateSummary(group: RoundHistory): string {
  if (group.findings.length === 0) return "";
  const counts = new Map<Fate, number>();
  for (const { fate } of group.findings) counts.set(fate, (counts.get(fate) ?? 0) + 1);
  if (counts.size === 1) return FATE_LABEL[group.findings[0]!.fate];
  return FATE_ORDER.filter((fate) => counts.has(fate))
    .map((fate) => `${counts.get(fate)} ${FATE_LABEL[fate]}`)
    .join(" · ");
}

/** A run's findings: one fold per review round, newest first and
 *  the newest open, each card carrying the fate the round's ledger row and
 *  the next round give it. The heading counts findings over rounds. */
function FindingsSection({ rounds, ledger, showHeading = true }: { rounds: Round[]; ledger: LedgerRow[]; showHeading?: boolean }) {
  const history = findingsHistory(rounds, ledger).sort((a, b) =>
    rounds.findIndex((round) => round.round === b.round) - rounds.findIndex((round) => round.round === a.round));
  const total = history.reduce((sum, group) => sum + group.findings.length, 0);
  return (
    <>
      {showHeading && <div className="mt-4 flex items-baseline gap-3">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">Findings</span>
        <span data-findings-count className="font-mono text-[12px] text-muted">
          {total} over {history.length} rounds
        </span>
      </div>}
      <div className="mt-2 flex flex-col gap-3">
        {history.map((group, index) => (
          <RoundFold key={group.round} ordinal={rounds.findIndex((round) => round.round === group.round) + 1} group={group} startOpen={index === 0} />
        ))}
      </div>
    </>
  );
}

/** One round's fold: "Round N · M findings · fates" behind the run log's
 *  disclosure, closed but for the newest. */
function RoundFold({ group, startOpen, ordinal }: { group: RoundHistory; startOpen: boolean; ordinal: number }) {
  const [open, setOpen] = useState(startOpen);
  const count = group.findings.length;
  const summary = fateSummary(group);
  return (
    <section data-round-fold={group.round}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((previous) => !previous)}
        className="flex items-baseline gap-2 text-left"
      >
        <span data-chevron={open ? "open" : "closed"} aria-hidden="true" className="text-muted">
          {open ? "▾" : "▸"}
        </span>
        <span className="text-[12px] font-semibold text-ink">
          {roundLabel(ordinal)} · {count} {count === 1 ? "finding" : "findings"}
          {summary !== "" ? ` · ${summary}` : ""}
        </span>
      </button>
      {open && count > 0 && (
        <ul className="mt-2 flex flex-col gap-2">
          {group.findings.map((history, index) => (
            <FindingCard
              key={`${history.finding.path}:${history.finding.line ?? ""}:${index}`}
              finding={history.finding}
              fate={history.fate}
              sentence={history.sentence}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
