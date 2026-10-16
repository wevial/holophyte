import { useId, type ReactNode } from "react";
import { COLUMN_HEADS, COLUMN_IDS, type Lane, type TicketCard } from "../tickets";
import { BoardCard } from "./BoardCard";
import { FoldChevron, FolderIcon, StoryIcon } from "./icons";

export const GRID = "grid grid-cols-5 gap-[18px]";

/** Where a lane's body goes: its id and fold, or `flat` for the one lane of None. */
export interface LaneBody {
  id?: string;
  hidden: boolean;
  flat: boolean;
}

export interface LaneBodyProps {
  lane: Lane;
  now: number;
  openId: string | null;
  onOpen: (card: TicketCard) => void;
  body: LaneBody;
}

const FLAT_CELL = "min-h-[520px] border border-rule-2 bg-paper p-2.5";

export function LaneGrid({ lane, now, openId, onOpen, body }: LaneBodyProps) {
  return (
    <div id={body.id} hidden={body.hidden} data-lane-grid className={`${GRID} ${body.flat ? "px-px" : "px-4 pt-4 pb-[18px]"}`}>
      {COLUMN_IDS.map((column) => (
        <ul key={column} aria-label={COLUMN_HEADS[column].label} className={`flex min-w-0 flex-col gap-2 ${body.flat ? FLAT_CELL : ""}`}>
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

/** One lane of the Tickets page around its body; a lane with no glyph is the flat lane of None. */
export function BoardLane({ lane, open, onToggle, children }: { lane: Lane; open: boolean; onToggle: () => void; children: (body: LaneBody) => ReactNode }) {
  const body = useId();
  if (lane.glyph == null) return children({ hidden: false, flat: true });
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
      {children({ id: body, hidden: !open, flat: false })}
    </section>
  );
}
