import type { ReactNode } from "react";
import type { StoryDecisionRow, StoryDetail, StoryWitnessRow } from "../../lib/contract.gen";
import { shortSha } from "../../lib/shipped";
import type { State } from "../state";
import { dayWord } from "../story";
import { Glyph } from "./AgentPanel";
import { STATE_ICONS } from "./StateDot";

interface Note {
  id: string;
  kind: "Decision" | "Witness";
  glyph: State;
  tone: string;
  text: string;
  meta: ReactNode;
  ms: number;
}

const VERDICT_LOOK: Record<NonNullable<StoryWitnessRow["verdict"]>, { tone: string; glyph: State }> = {
  green: { tone: "text-live", glyph: "ready_to_merge" },
  red: { tone: "text-bad", glyph: "failed" },
  error: { tone: "text-bad", glyph: "failed" },
  absent: { tone: "text-ink-3", glyph: "abandoned" },
};

function decisionNote(decision: StoryDecisionRow, now: number): Note {
  const id = `decision ${decision.id}`;
  if (decision.answer == null) {
    return { id, kind: "Decision", glyph: "needs_you", tone: "text-need", text: decision.question, meta: "open · needs you", ms: decision.at };
  }
  const ms = decision.answered_ms ?? decision.at;
  const who = decision.answered_by ?? "answered";
  return { id, kind: "Decision", glyph: "ready_to_merge", tone: "text-act", text: `${decision.question} → ${decision.answer}`, meta: `${who} · ${dayWord(ms, now)}`, ms };
}

function witnessNote(witness: StoryWitnessRow, now: number): Note {
  const id = `witness ${witness.key}`;
  const text = `${witness.key} · ${witness.criterion}`;
  if (witness.verdict == null || witness.at == null) {
    return { id, kind: "Witness", glyph: "backlog", tone: "text-ink-3", text, meta: "not run yet", ms: -Infinity };
  }
  const sha = shortSha(witness.main_sha);
  const meta = (
    <>
      {witness.verdict}
      {sha !== "" && <> at <span className="font-mono">{sha}</span></>}
      {` · ${dayWord(witness.at, now)}`}
    </>
  );
  return { id, kind: "Witness", ...VERDICT_LOOK[witness.verdict], text, meta, ms: witness.at };
}

/** The story's decisions and witnesses, newest first; a witness never run last. */
export function StoryNotes({ story, now }: { story: StoryDetail; now: number }) {
  const notes = [...story.decisions.map((each) => decisionNote(each, now)), ...story.witnesses.map((each) => witnessNote(each, now))].sort((a, b) => b.ms - a.ms);
  return (
    <section aria-labelledby="story-notes" className="mt-5 w-full max-w-[1320px]">
      <h2 id="story-notes" className="flex h-[30px] items-center gap-2.5 border-b border-rule-2 font-sans text-[13px] font-semibold text-ink">
        Decisions and witnesses <span data-note-count className="font-mono font-normal text-ink-3">{notes.length}</span>
      </h2>
      {notes.length === 0 ? (
        <p className="mt-2.5 flex items-center gap-2 font-sans text-[13px] text-ink-3">
          <span className="text-live"><Glyph>{STATE_ICONS.ready_to_merge}</Glyph></span>
          No decisions or witnesses yet.
        </p>
      ) : (
        <ul>
          {notes.map((note) => (
            <li
              key={note.id}
              data-note={note.id}
              className="grid min-h-[34px] grid-cols-[96px_minmax(0,1fr)_auto] items-baseline gap-x-3.5 gap-y-1 border-b border-rule-2 py-2 max-[720px]:grid-cols-1"
            >
              <span data-note-kind className={`inline-flex items-center gap-1.5 font-sans text-[12.5px] ${note.tone}`}>
                <Glyph>{STATE_ICONS[note.glyph]}</Glyph>
                {note.kind}
              </span>
              <span className="max-w-[75ch] font-sans text-[14px] leading-[1.45] [overflow-wrap:anywhere] text-ink">{note.text}</span>
              <span className="whitespace-nowrap font-sans text-[12px] text-ink-3">{note.meta}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
