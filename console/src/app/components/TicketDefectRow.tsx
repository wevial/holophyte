import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import type { TicketDefect } from "../../lib/contract.gen";
import { formatStamp } from "../../lib/format";
import type { DefectView } from "../findings";
import { keepScope } from "../scope";
import type { ScoutTarget } from "../scouts";
import { ScoutThis } from "./ScoutThis";
import { TONE_TEXT } from "./StateDot";

const STOP: Record<TicketDefect["kind"], string> = {
  contradiction: "contradiction",
  criterion_stuck: "stuck criterion",
  review_stuck: "stuck review",
};

/** Past this many characters a one-line note is taken to overflow its line. */
const NOTE_LINE = 96;

function RequeueNote({ note }: { note: string }) {
  const [open, setOpen] = useState(false);
  const noteId = useId();
  const more = note.includes("\n") || note.length > NOTE_LINE;
  return (
    <div className="flex items-baseline gap-x-3 pl-1">
      <p id={noteId} data-note className={`m-0 min-w-0 flex-1 font-read text-[14px] leading-[1.5] text-ink-2 ${open || !more ? "whitespace-pre-line [overflow-wrap:anywhere]" : "truncate"}`}>
        {open ? note : note.split("\n")[0]}
      </p>
      {more && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={noteId}
          onClick={() => setOpen(!open)}
          className="shrink-0 cursor-pointer border-0 bg-transparent p-0 font-mono text-[12px] text-ink-2 underline underline-offset-2 hover:text-ink"
        >
          {open ? "Less" : "More"}
        </button>
      )}
    </div>
  );
}

function defectScout(defect: DefectView): ScoutTarget {
  const ask = `What in ${defect.ticket}'s wording stopped run ${defect.run}, and how should it read?`;
  return { project: defect.project, about: `defect:${defect.run}`, kind: "research", ask, off: defect.actions ? null : ACTIONS_OFF };
}

/** A run that stopped on a wrong ticket: its ticket, criteria and stop, the revision that fixed the ticket, and the requeue note. */
export function TicketDefectRow({ defect }: { defect: DefectView }) {
  return (
    <li data-defect-row className="flex flex-col gap-1 border-b border-rule-2 px-1 py-2">
      <div className="flex min-h-9 flex-wrap items-center gap-x-3.5 gap-y-1.5">
        <Link
          to="/tickets/$project/$key"
          params={{ project: defect.project, key: defect.ticket }}
          search={keepScope}
          className="font-mono text-[12.5px] font-medium text-ink no-underline hover:underline"
        >
          {defect.ticket}
        </Link>
        {defect.criteria.map((criterion) => (
          <span key={criterion} data-tag className="px-1.5 py-[3px] font-mono text-[11px] font-medium leading-none text-ink-2 shadow-[inset_0_0_0_1px_var(--rule)]">
            {criterion}
          </span>
        ))}
        <span data-stop className={`font-sans text-[13px] font-medium ${TONE_TEXT.need}`}>{STOP[defect.kind]}</span>
        <span data-place className="font-mono text-[12px] text-ink-3">
          revision {defect.revision} · {formatStamp(defect.revised_ms)}
        </span>
        <span className="ml-auto">
          <ScoutThis target={defectScout(defect)} />
        </span>
      </div>
      {defect.requeue_note != null && <RequeueNote note={defect.requeue_note} />}
    </li>
  );
}
