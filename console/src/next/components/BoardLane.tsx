import { useId } from "react";
import { COLUMN_HEADS, COLUMN_IDS, type Lane, type TicketCard } from "../tickets";
import { BoardCard } from "./BoardCard";
import { FoldChevron, FolderIcon, StoryIcon } from "./icons";

export const GRID = "grid grid-cols-5 gap-[18px]";

interface LaneProps {
  lane: Lane;
  now: number;
  openId: string | null;
  onOpen: (card: TicketCard) => void;
}

function LaneGrid({ lane, now, openId, onOpen, id, hidden = false, className }: LaneProps & { id?: string; hidden?: boolean; className: string }) {
  return (
    <div id={id} hidden={hidden} data-lane-grid className={`${GRID} ${className}`}>
      {COLUMN_IDS.map((column) => (
        <ul key={column} aria-label={COLUMN_HEADS[column].label} className="flex min-w-0 flex-col gap-2">
          {lane.columns[column].map((card) => (
            <li key={card.id}>
              <BoardCard card={card} column={column} now={now} open={card.id === openId} onOpen={onOpen} />
            </li>
          ))}
        </ul>
      ))}
    </div>
  );
}

/** One lane of the board; a lane with no name is the flat board of None. */
export function BoardLane({ open, onToggle, ...props }: LaneProps & { open: boolean; onToggle: () => void }) {
  const { lane } = props;
  const body = useId();
  if (lane.name === "") return <LaneGrid {...props} className="" />;
  const glyphTone = lane.glyph === "folder" ? (lane.held ? "text-wait" : "text-live") : "text-ink-2";
  return (
    <section aria-label={lane.name} className="border border-rule-2 bg-paper">
      <h2 className={`px-2 py-1 ${open ? "border-b border-rule-2" : ""}`}>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={body}
          onClick={onToggle}
          className="group flex min-h-11 w-full cursor-pointer items-center gap-2.5 px-2 text-left font-mono text-[13px] font-medium leading-[1.2] text-ink hover:bg-paper-2"
        >
          <FoldChevron open={open} />
          {lane.glyph === "folder" ? <FolderIcon className={glyphTone} /> : <StoryIcon className={glyphTone} />}
          <span className="min-w-0 [overflow-wrap:anywhere]">{lane.name}</span>
          <span data-lane-meta className="shrink-0 font-normal text-ink-3">{lane.meta}</span>
          {lane.needText !== "" && <span data-lane-need className="ml-auto shrink-0 text-need">{lane.needText}</span>}
        </button>
      </h2>
      <LaneGrid {...props} id={body} hidden={!open} className="px-4 pt-4 pb-[18px]" />
    </section>
  );
}
