import { Link } from "@tanstack/react-router";
import { useContext, useState } from "react";
import type { Round, RunDetail, RunTurn } from "../../lib/contract.gen";
import { formatClock, formatDuration } from "../../lib/format";
import { AnswerError } from "../../lib/poll";
import { useRunFiles, useRunTurns } from "../queries";
import { PollDepsContext } from "../scope";
import { CRUMB_LINK } from "../pages/runShared";
import { turnPage, turnTitles } from "../turn";
import { SECTION_LABEL } from "./Criteria";

const ROW = "flex justify-between gap-3 border-b border-dashed border-rule-2 py-1 font-mono text-[12.5px] leading-[1.8]";

function ReviewRound({ round, max }: { round: Round; max: number }) {
  const live = round.ended_ms == null;
  return <li className="border-b border-dashed border-rule-2 py-2.5 text-[14px]">
    <div>
      <strong className="font-medium">Round {round.round}</strong>: {live ? "in progress" : round.verdict === "pass" ? "passed" : round.verdict.replaceAll("_", " ")}
      <span className="font-mono text-[12px] text-ink-3">
        {live
          ? ` · started ${formatClock(round.started_ms)} · max ${max} rounds`
          : `${round.reviewer_model ? ` · ${round.reviewer_model}` : ""} · ${formatClock(round.ended_ms!)} · ${round.findings.length} ${round.findings.length === 1 ? "finding" : "findings"}`}
      </span>
    </div>
    {round.findings.map((finding, index) => <p key={index} className="m-0 mt-1">
      <span className="font-mono text-[11px] font-semibold text-need">{finding.severity.toUpperCase()}</span>{" "}{finding.message}
    </p>)}
  </li>;
}

function Turns({ project, run, turns }: { project: string; run: number; turns: RunTurn[] }) {
  return <ol className="m-0 list-none p-0">
    {turnTitles(turns).map(({ turn, seat, ordinal }) => <li key={turn.id} className={ROW}>
      <Link {...turnPage(project, run, turn.id)} className={CRUMB_LINK}>{seat} · {turn.label ?? "label unknown"}{ordinal && ` · ${ordinal}`}</Link>
      <span className="shrink-0">{turn.seconds == null ? "running" : formatDuration(turn.seconds * 1000)}</span>
    </li>)}
  </ol>;
}

export function RunDetails({ project, base, detail, live }: { project: string; base: string; detail: RunDetail; live: boolean }) {
  const [open, setOpen] = useState(false);
  const deps = useContext(PollDepsContext);
  const files = useRunFiles(base, detail.run.id, open, live, deps);
  const turns = useRunTurns(base, detail.run.id, open, live, deps);
  const fileError = files.error instanceof AnswerError && files.error.status === 409
    ? "No branch or merge to diff."
    : files.error instanceof AnswerError && files.error.status === 504
      ? "git did not answer in time."
      : "Could not load files.";
  return <details onToggle={(event) => setOpen(event.currentTarget.open)} className="mt-2 border-t border-rule text-ink-2">
    <summary className="min-h-11 cursor-pointer py-3 font-mono text-[11px] font-medium tracking-[.14em]">
      <span className="uppercase">Details</span>{" "}
      <span className="tracking-[.04em] text-ink-3">files · review rounds · turns</span>
    </summary>
    {open && <div className="flex flex-col gap-[18px] pb-2 pt-3">
      <section aria-label="Files changed">
        <h3 className={`${SECTION_LABEL} m-0 mb-2`}>Files changed{files.data && ` · ${files.data.files.length}${files.data.truncated ? " shown" : ""}`}</h3>
        {files.isPending && <p role="status">Loading files…</p>}
        {files.isError ? <p role="alert">{fileError}</p> : files.data && <>
          {files.data.files.length === 0 && <p>No files changed yet.</p>}
          <ul className="m-0 list-none p-0">
            {files.data.files.map((file) => <li key={file.path} className={ROW}>
              <span className="min-w-0 [overflow-wrap:anywhere]">{file.path}</span>
              <span className="shrink-0"><span className="text-live">+{file.added}</span>{" "}<span className="text-need">−{file.deleted}</span></span>
            </li>)}
          </ul>
        </>}
      </section>
      <section aria-label="Review rounds">
        <h3 className={`${SECTION_LABEL} m-0 mb-2`}>Review rounds</h3>
        <ol className="m-0 list-none p-0">
          {detail.rounds.map((round) => <ReviewRound key={round.round} round={round} max={detail.run.max_rounds} />)}
        </ol>
      </section>
      <section aria-label="Transcript">
        <h3 className={`${SECTION_LABEL} m-0 mb-2`}>Transcript{turns.data && ` · ${turns.data.turns.length} turns`}</h3>
        {turns.isPending && <p role="status">Loading turns…</p>}
        {turns.isError ? <p role="alert">Could not load turns.</p> : turns.data && <Turns project={project} run={detail.run.id} turns={turns.data.turns} />}
      </section>
    </div>}
  </details>;
}
