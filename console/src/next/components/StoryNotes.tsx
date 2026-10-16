import type { StoryDecisionRow, StoryDetail, StoryWitnessRow } from "../../lib/contract.gen";
import { shortSha } from "../../lib/shipped";
import { dayWord } from "../story";

interface Note {
  id: string;
  kind: "DECISION" | "WITNESS";
  tone: string;
  text: string;
  meta: string;
  ms: number;
}

const VERDICT_TONE: Record<NonNullable<StoryWitnessRow["verdict"]>, string> = {
  green: "text-live",
  red: "text-bad",
  error: "text-bad",
  absent: "text-ink-3",
};

function decisionNote(decision: StoryDecisionRow, now: number): Note {
  const id = `decision ${decision.id}`;
  if (decision.answer == null) {
    return { id, kind: "DECISION", tone: "text-need", text: decision.question, meta: "open · needs you", ms: decision.at };
  }
  const ms = decision.answered_ms ?? decision.at;
  const who = decision.answered_by ?? "answered";
  return { id, kind: "DECISION", tone: "text-act", text: `${decision.question} → ${decision.answer}`, meta: `${who} · ${dayWord(ms, now)}`, ms };
}

function witnessNote(witness: StoryWitnessRow, now: number): Note {
  const id = `witness ${witness.key}`;
  const text = `${witness.key} · ${witness.criterion}`;
  if (witness.verdict == null || witness.at == null) {
    return { id, kind: "WITNESS", tone: "text-ink-3", text, meta: "not run yet", ms: -Infinity };
  }
  const sha = shortSha(witness.main_sha);
  const meta = `${witness.verdict}${sha === "" ? "" : ` at ${sha}`} · ${dayWord(witness.at, now)}`;
  return { id, kind: "WITNESS", tone: VERDICT_TONE[witness.verdict], text, meta, ms: witness.at };
}

/** The story's decisions and witnesses, newest first; a witness never run last. */
export function StoryNotes({ story, now }: { story: StoryDetail; now: number }) {
  const notes = [...story.decisions.map((each) => decisionNote(each, now)), ...story.witnesses.map((each) => witnessNote(each, now))].sort((a, b) => b.ms - a.ms);
  return (
    <section aria-labelledby="story-notes" className="mt-5 border border-rule bg-paper px-5 pb-2 pt-4 max-[720px]:px-4">
      <h2 id="story-notes" className="font-sans text-[11px] font-semibold uppercase leading-none tracking-[0.06em] text-ink-3">
        Decisions and witnesses <span data-note-count className="tracking-[0.04em] text-ink">{notes.length}</span>
      </h2>
      {notes.length === 0 ? (
        <p className="py-3 text-[14px] text-ink-2">No decisions or witnesses yet.</p>
      ) : (
        <ul className="mt-2">
          {notes.map((note) => (
            <li
              key={note.id}
              data-note={note.id}
              className="grid grid-cols-[96px_minmax(0,1fr)_auto] items-baseline gap-x-3.5 gap-y-1 border-b border-rule-2 py-2.5 text-[14px] leading-[1.45] max-[720px]:grid-cols-1"
            >
              <span data-note-kind className={`font-mono text-[11px] font-medium leading-[1.4] tracking-[0.1em] ${note.tone}`}>{note.kind}</span>
              <span className="[overflow-wrap:anywhere] text-ink">{note.text}</span>
              <span className="whitespace-nowrap font-mono text-[12px] leading-[1.4] text-ink-3">{note.meta}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
