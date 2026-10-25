import { Markdown } from "./Markdown";
import type { OperatorNote, Round, RoundInstruction, RunEvent } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";

const HEAD = "font-mono text-[11px] font-medium tracking-[.1em] text-ink-2";
const BODY = "[&_:is(p,ul,ol,pre,blockquote)+*]:mt-2 [&_p]:m-0 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_code]:font-mono [&_code]:text-[12.5px] [&_pre]:whitespace-pre-wrap [&_a]:underline";

function noteTime(note: OperatorNote, round: Round, events: RunEvent[]): number {
  return events.find((event) => event.kind === "operator_note_consumed" &&
    event.summary === `operator_note event ${note.event_id} drove round ${round.round}`)?.at ?? round.started_ms;
}

function Instruction({ instruction }: { instruction: RoundInstruction }) {
  const location = instruction.line != null ? `${instruction.path}:${instruction.line}` : instruction.path;
  const state = instruction.outcome ?? "awaiting fix";
  const tone = state === "changed" ? "border-fact-ok text-fact-ok" : state === "kept" ? "border-ink-3 text-ink-3" : "border-fact-warn text-fact-warn";
  return <li data-instruction className="border-t border-dashed border-rule-2 py-1.5">
    <div className="flex flex-wrap items-baseline gap-x-2 font-mono text-[12px]">
      <span data-outcome={state} className={`border px-1.5 text-[11px] font-semibold ${tone}`}>{state}</span>
      {instruction.url
        ? <a href={instruction.url} className="min-w-0 underline [overflow-wrap:anywhere]">{location}</a>
        : <span className="min-w-0 [overflow-wrap:anywhere]">{location}</span>}
      <span className="text-ink-3">@{instruction.author}</span>
    </div>
    {instruction.triage && <p data-triage className="m-0 mt-1 font-mono text-[11px] text-ink-3">
      Triage: {instruction.triage.decision} · confidence {instruction.triage.confidence ?? "unavailable"} · {instruction.triage.route} · {instruction.triage.reason}
    </p>}
    <div className={`mt-1 ${BODY}`}><Markdown>{instruction.request}</Markdown></div>
  </li>;
}

export function RoundInstructions({ round, events }: { round: Round; events: RunEvent[] }) {
  return <>
    {round.operator_notes.map((note) => <div key={note.event_id} data-operator-note className="mt-2 border-l-2 border-ink-3 pl-3">
      <div className={HEAD}>Note · @{note.author} · {formatClock(noteTime(note, round, events))}</div>
      <div className={`mt-1 ${BODY}`}><Markdown>{note.note}</Markdown></div>
    </div>)}
    {round.instructions.length > 0 && <div data-pr-comments className="mt-2">
      <div className={HEAD}>PR comments · {round.instructions.length}</div>
      <ul className="m-0 mt-1 list-none p-0">
        {round.instructions.map((instruction, index) => <Instruction key={index} instruction={instruction} />)}
      </ul>
    </div>}
  </>;
}
