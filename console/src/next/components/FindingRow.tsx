import { useContext, useId, useState } from "react";
import { fileTicket } from "../../lib/boardWrites";
import type { Severity } from "../../lib/findings";
import { useAction } from "../actions";
import type { FindingRowView, RowAction, RowFate } from "../findings";
import { PollDepsContext } from "../scope";
import { Btn } from "./Btn";
import { FactIcon, FoldChevron, FolderIcon } from "./icons";
import { StateDot, TONE_TEXT } from "./StateDot";
import { useToast } from "./Toast";

const PILL: Record<Severity, { text: string; className: string }> = {
  must: { text: "P1", className: "bg-p1 text-[#171a2c]" },
  should: { text: "P2", className: "bg-p2 text-[#171a2c]" },
  nit: { text: "P3", className: "bg-p3 text-p3-ink" },
};

const lastTwo = (path: string) => path.split("/").filter(Boolean).slice(-2).join("/");

const steeredFate = (ticket: string): RowFate => ({
  state: "running",
  word: "steered into the next run",
  line: `steered · ${ticket} reads it on its next run`,
  tone: "live",
});

const filedFate = (ticket: string): RowFate => ({ state: "ready_to_merge", word: "filed", line: `filed as ${ticket} · Backlog`, tone: "live" });

/** The row's button and what it posts: a taken post hands `onDone` the
 *  row's new fate, a refusal says its reason in the toast and leaves the
 *  button enabled. */
function RowActionButton({ action, onDone }: { action: RowAction; onDone: (fate: RowFate) => void }) {
  const steer = useAction();
  const deps = useContext(PollDepsContext);
  const toast = useToast();
  const [filing, setFiling] = useState(false);

  const press = async () => {
    if (action.kind === "steer") {
      const said = `Steered ${action.ticket} with the finding.`;
      const body = { ticket: action.ticket, note: action.note, hint: false };
      const result = await steer.mutateAsync({ label: "Steer", base: action.base, route: "/actions/steer", body, said });
      if (result.ok) onDone(steeredFate(action.ticket));
      else toast(result.detail);
      return;
    }
    setFiling(true);
    const result = await fileTicket(action.base, action.body, null, deps.fetch, "backlog");
    setFiling(false);
    if (result.ok) {
      toast(`Filed ${result.ticket} in Backlog.`);
      onDone(filedFate(result.ticket));
    } else {
      toast("problems" in result ? (result.problems[0] ?? "The daemon refused the ticket.") : result.error);
    }
  };

  const steering = action.kind === "steer";
  return (
    <Btn
      busy={steering ? steer.isPending : filing}
      busyLabel={steering ? "Steering…" : "Filing…"}
      disabled={action.off != null}
      title={action.off ?? undefined}
      onClick={action.off != null ? undefined : () => void press()}
    >
      {steering ? "Steer into next run" : "File ticket"}
    </Btn>
  );
}

/** One finding, follow-up or advisory thread on a line that folds open to
 *  its text, place and fate, and its one action. A taken action's fate
 *  stands while the page is mounted. */
export function FindingRow({ row, showProject, quiet = false }: { row: FindingRowView; showProject: boolean; quiet?: boolean }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<RowFate | null>(null);
  const body = useId();
  const pill = row.severity == null ? null : PILL[row.severity];
  const fate = done ?? row.fate;
  return (
    <li data-finding-row className="border-b border-rule-2">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={body}
        onClick={() => setOpen(!open)}
        className="flex min-h-[52px] w-full flex-wrap items-center gap-x-3.5 gap-y-1.5 px-1 py-1.5 text-left transition-colors duration-(--motion-colour) hover:bg-paper-2"
      >
        <span className="text-ink-3"><FoldChevron open={open} /></span>
        <span className="flex w-24 shrink-0 gap-1">
          {pill && <span data-pill className={`px-1.5 py-[3px] font-mono text-[11px] font-semibold leading-none ${pill.className}`}>{pill.text}</span>}
          {row.tag && (
            <span
              data-tag
              className={`px-1.5 py-[3px] font-mono text-[11px] font-medium leading-none ${row.tag.filled ? "bg-tag text-[#171a2c]" : "text-ink-2 shadow-[inset_0_0_0_1px_var(--rule)]"}`}
            >
              {row.tag.text}
            </span>
          )}
        </span>
        <span data-title className="min-w-0 flex-[1_1_260px] font-plex text-[15px] font-medium [overflow-wrap:anywhere] text-ink">{row.title}</span>
        {showProject && (
          <span data-project className="inline-flex items-center gap-1.5 font-mono text-[12px] text-ink-2">
            <FolderIcon />
            {row.project}
          </span>
        )}
        {row.path != null && <span data-path className="min-w-0 font-mono text-[12px] [overflow-wrap:anywhere] text-ink-3">{lastTwo(row.path)}</span>}
        <StateDot state={fate.state} label={fate.word} hideWord tone={quiet ? "ink-3" : undefined} />
      </button>
      <div id={body} hidden={!open} className="pb-4 pl-[142px] pr-1 pt-0.5 max-[720px]:pl-1">
        {open && (
          <div className="flex flex-wrap items-end justify-between gap-x-5 gap-y-2.5">
            <div className="min-w-0 font-plex text-[14px] leading-[1.5] [overflow-wrap:anywhere]">
              {row.text !== "" && <p data-text className="whitespace-pre-line text-ink-2">{row.text}</p>}
              <p data-place className="mt-1 font-mono text-[12px] text-ink-3">{row.place}</p>
              <p data-fate className={`mt-1 font-mono text-[12px] font-medium leading-[1.4] ${TONE_TEXT[fate.tone]}`}>
                {done == null ? fate.line : <span className="stamp">{fate.line}</span>}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              {row.href != null && (
                <a href={row.href} className="border-b border-rule pb-0.5 font-mono text-[12px] font-medium leading-[1.3] text-ink-2 no-underline hover:text-ink">
                  Open run
                </a>
              )}
              {row.action != null && done == null && <RowActionButton action={row.action} onDone={setDone} />}
            </div>
          </div>
        )}
      </div>
    </li>
  );
}

/** A review round that raised nothing, and who reviewed it. */
export function CleanReviewLine({ ticket, round, reviewer }: { ticket: string; round: number; reviewer: string | null }) {
  return (
    <li data-clean className="flex items-baseline gap-2 py-1.5">
      <span className="self-center"><FactIcon ok /></span>
      <p className="min-w-0 font-plex text-[14px] text-ink-2 [overflow-wrap:anywhere]">
        <span data-sentence>{`${ticket} passed round ${round} with no findings.`}</span>
        {reviewer != null && <span data-reviewer className="ml-2 font-mono text-[12px] text-ink-3">reviewed by {reviewer}</span>}
      </p>
    </li>
  );
}
