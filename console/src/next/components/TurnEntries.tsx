import { Link } from "@tanstack/react-router";
import { useContext } from "react";
import type { RunTurn } from "../../lib/contract.gen";
import { AnswerError } from "../../lib/poll";
import { useTurnTranscript } from "../queries";
import { runPage } from "../run";
import { PollDepsContext } from "../scope";
import { transcriptRows, type TranscriptRow } from "../turn";
import { TranscriptIcon } from "./icons";

const LABEL = "mb-2 flex items-center gap-2 font-mono text-[11px] uppercase tracking-[.1em]";
const PROSE = "m-0 max-w-[72ch] whitespace-pre-wrap font-read text-[15px] leading-[1.6] [overflow-wrap:anywhere]";

function TextBox({ label, text, cut, failed = false }: { label: string; text: string; cut: boolean; failed?: boolean }) {
  return <div className="mt-3">
    <div className="mb-1 font-mono text-[11px] uppercase tracking-[.1em] text-ink-3">{label}</div>
    <pre className={`m-0 whitespace-pre-wrap border bg-paper-2 p-3 font-mono text-[12px] leading-[1.6] text-ink-2 [overflow-wrap:anywhere] ${failed ? "border-bad" : "border-rule-2"}`}>
      {text}{cut && <span className="text-ink-3">{"\n"}cut at 20,000 characters</span>}
    </pre>
  </div>;
}

function Fold({ row }: { row: Extract<TranscriptRow, { kind: "tool" }> | Extract<TranscriptRow, { text: string }> }) {
  const tool = row.kind === "tool" ? row : null;
  const prompt = row.kind === "prompt" ? row : null;
  return <details open={tool?.failed || undefined} className="group min-w-0">
    <summary className="flex cursor-pointer list-none items-center gap-3 font-mono text-[12px] [&::-webkit-details-marker]:hidden">
      <span aria-hidden="true" className="text-ink-3 group-open:rotate-90">›</span>
      <strong className="shrink-0 font-semibold text-ink">{tool?.name ?? "Prompt"}</strong>
      <span className="min-w-0 flex-1 truncate text-ink-2">{tool?.summary ?? prompt?.text.split("\n")[0]}</span>
      <span className={`shrink-0 ${tool ? tool.failed ? "text-bad" : "text-live" : "text-ink-3"}`}>
        {tool ? tool.failed ? "failed" : "ok" : "sent"}{tool?.duration != null && ` · ${tool.duration.toFixed(1)}s`}
      </span>
    </summary>
    {prompt && <TextBox label="Input" text={prompt.text} cut={prompt.cut} />}
    {tool?.input && <TextBox label="Input" text={tool.input.text} cut={tool.input.cut} />}
    {tool?.output && <TextBox label="Output" text={tool.output.text} cut={tool.output.cut} failed={tool.failed} />}
  </details>;
}

function Entry({ row }: { row: TranscriptRow }) {
  const failed = row.kind === "error" || (row.kind === "tool" && row.failed);
  const label = { prompt: "Factory", note: "Factory", said: "Assistant", tool: "Tool call", error: "Error", end: "End" }[row.kind];
  const tone = row.kind === "error" ? "text-bad" : row.kind === "tool" ? "text-wait" : row.kind === "said" ? "text-ink-2" : "text-ink-3";
  return <li className={`grid grid-cols-[64px_minmax(0,1fr)] gap-3 border-rule-2 px-5 py-4 [&+li]:border-t max-[720px]:grid-cols-1 max-[720px]:gap-2 max-[720px]:px-3 ${failed ? "bg-bad-wash shadow-[inset_2px_0_0_var(--bad)]" : ""}`}>
    <span className="font-mono text-[12px] leading-4 text-ink-3">{row.offset}</span>
    <div className="min-w-0">
      <div className={`${LABEL} ${tone}`}><TranscriptIcon kind={row.kind} />{label}</div>
      {row.kind === "tool" || row.kind === "prompt" ? <Fold row={row} /> :
        <p className={`${PROSE} ${row.kind === "error" ? "text-bad" : "text-ink-2"}`}>{row.text}
          {"cut" in row && row.cut && <span className="block font-mono text-[12px] text-ink-3">cut at 20,000 characters</span>}
        </p>}
    </div>
  </li>;
}

export function TurnEntries({ base, project, run, host, turn }: { base: string; project: string; run: number; host: string | null; turn: RunTurn }) {
  const deps = useContext(PollDepsContext);
  const answer = useTurnTranscript(base, run, turn.id, deps);
  const name = host ?? project;
  if (answer.isPending) return <section className="mt-5">
    <p role="status" className="font-mono text-[12px] text-ink-3">Fetching the transcript from {name}…</p>
    <div aria-hidden="true" className="space-y-3 border border-rule bg-paper p-5">
      {[100, 80, 90, 60, 75].map((width, index) => <div key={index} className="h-4 bg-rule-2 motion-safe:animate-pulse" style={{ width: `${width}%` }} />)}
    </div>
  </section>;
  if (answer.isError) return <section className="mt-5">
    {answer.error instanceof AnswerError && answer.error.status === 404 ? <>
      <p className={PROSE}>{name} has no transcript for this turn.</p>
      <p className="font-mono text-[12px] text-ink-3">The run's Progress log still has what this turn did; the figures above come from the run record.</p>
      <Link {...runPage(project, run)} className="btn" data-variant="quiet">Open the run</Link>
    </> : <p role="alert" className={PROSE}>Could not read the transcript from {name}.</p>}
  </section>;
  if (!answer.data.entries.length) return <p className={`mt-5 ${PROSE}`}>This turn has no entries.{turn.timed_out && " It timed out before the agent wrote anything."}</p>;
  return <section className="mt-5">
    <ol aria-label="Turn transcript" className="m-0 list-none border border-rule bg-paper p-0">
      {transcriptRows(answer.data.entries, turn).map((row, index) => <Entry key={index} row={row} />)}
    </ol>
    <p className="mt-4 border-t border-rule pt-3 font-mono text-[12px] leading-[1.6] text-ink-3">Times are from the start of the turn. Tool input and output fold; failures open on their own.</p>
  </section>;
}
