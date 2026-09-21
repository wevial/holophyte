import { formatClock } from "../lib/format";
import { Markdown } from "./Markdown";
import { roundLabel } from "../lib/runs";
import type { Round } from "../lib/types";

export function OperatorNoteCard({ note, ordinal, started }: {
  note: NonNullable<Round["operator_notes"]>[number];
  ordinal: number;
  /** When the note started its fix round; the wire omits its creation time. */
  started: number;
}) {
  return (
    <article data-operator-note className="mt-2 rounded-[10px] border border-line bg-card px-4 py-[14px] shadow-card">
      <header className="flex flex-wrap items-center gap-2 text-[12px] text-muted">
        <span>@{note.author}</span>
        <span>{roundLabel(ordinal)}</span>
        <span>started <time dateTime={new Date(started).toISOString()}>{formatClock(started)}</time></span>
        <span className="font-mono text-[11px]">event {note.event_id}</span>
      </header>
      <div data-note-body className="mt-1 text-[14px] leading-[1.45] text-body">
        <Markdown>{note.note}</Markdown>
      </div>
    </article>
  );
}
