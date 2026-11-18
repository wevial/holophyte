import { Link } from "@tanstack/react-router";
import { formatAge } from "../../lib/format";
import { STATES, type Tone } from "../state";
import { cardState, cardWord, COLUMN_HEADS, type ColumnId, type Lane, type TicketCard } from "../tickets";
import { keepScope } from "../scope";
import { cardElementId, modified } from "./BoardCard";
import { BoardLane, type LaneBodyProps } from "./BoardLane";
import { StateDot, TONE_TEXT } from "./StateDot";
import { TitleText } from "./TitleText";

const ROW_ORDER: ColumnId[] = ["needs_you", "in_flight", "ready", "backlog", "done"];

const byKey = (a: TicketCard, b: TicketCard) => a.key.localeCompare(b.key, undefined, { numeric: true });

const GRID = "grid grid-cols-[132px_96px_minmax(0,1fr)_220px_40px] items-center gap-4 max-[820px]:grid-cols-[auto_auto_minmax(0,1fr)]";

const columnTone = (column: ColumnId): Tone => (column === "needs_you" ? "need" : STATES[COLUMN_HEADS[column].state].tone);

function TicketRow({ card, column, now, open, onOpen }: { card: TicketCard; column: ColumnId; now: number; open: boolean; onOpen: (card: TicketCard) => void }) {
  const tone = columnTone(column);
  const quiet = column === "backlog" || column === "done";
  return (
    <Link
      data-row
      id={cardElementId(card)}
      to="/tickets/$project/$key"
      params={{ project: card.project, key: card.key }}
      search={keepScope}
      aria-haspopup="dialog"
      data-open={open || undefined}
      data-column={column}
      onClick={(event) => {
        if (modified(event) || event.button !== 0) return;
        event.preventDefault();
        onOpen(card);
      }}
      className={`${GRID} min-h-[34px]! px-4 py-1.5 font-sans text-[13px] leading-[1.3] text-ink-2 no-underline transition-colors duration-(--motion-colour) ${open ? "bg-selected" : "hover:bg-[color-mix(in_srgb,var(--hover)_50%,var(--raised))]"}`}
    >
      <span data-cell="status" className={`inline-flex items-center gap-[5px] whitespace-nowrap text-[12.5px] ${open ? "text-ink" : TONE_TEXT[tone]}`}>
        <StateDot state={COLUMN_HEADS[column].state} hideWord tone={tone} />
        {COLUMN_HEADS[column].label}
      </span>
      <span data-cell="key" className={`font-mono text-[12px] tabular-nums ${open ? "text-ink-2" : "text-ink-3"}`}>{card.key}</span>
      <span
        data-cell="title"
        className={`min-w-0 truncate font-medium leading-[1.35] max-[820px]:col-span-full max-[820px]:row-start-2 max-[820px]:whitespace-normal ${quiet && !open ? "text-ink-2" : "text-ink"}`}
      >
        <TitleText>{card.title}</TitleText>
      </span>
      <span data-cell="stage" className={`truncate text-[12.5px] max-[820px]:hidden ${open ? "text-ink" : TONE_TEXT[STATES[cardState(card)].tone]}`}>{cardWord(card, now)}</span>
      <span data-cell="estimate" className={`text-right font-mono text-[12px] tabular-nums max-[820px]:hidden ${open ? "text-ink-2" : "text-ink-3"}`}>
        {card.timeBoxMs != null && (
          <>
            {formatAge(card.timeBoxMs)}
            <span className="sr-only"> time box</span>
          </>
        )}
      </span>
    </Link>
  );
}

function LaneRows({ lane, now, openId, onOpen, body }: LaneBodyProps) {
  return (
    <ul id={body.id} hidden={body.hidden} data-lane-rows className={body.flat ? "" : "pb-1.5"}>
      {ROW_ORDER.flatMap((column) =>
        lane.columns[column].toSorted(byKey).map((card) => (
          <li key={card.id} className="border-b border-hair last:border-b-0">
            <TicketRow card={card} column={column} now={now} open={card.id === openId} onOpen={onOpen} />
          </li>
        )),
      )}
    </ul>
  );
}

/** The lanes' tickets as rows, Needs you first, each column's rows by ticket key. */
export function TicketTable({
  lanes,
  now,
  openId,
  onOpen,
  folded,
  onFold,
}: {
  lanes: Lane[];
  now: number;
  openId: string | null;
  onOpen: (card: TicketCard) => void;
  folded: ReadonlySet<string>;
  onFold: (id: string) => void;
}) {
  return (
    <div data-ticket-table>
      <div data-table-heads className={`${GRID} min-h-[30px] border-b border-hair px-4 text-[12px] font-medium leading-none text-ink-3 max-[820px]:hidden`}>
        <span>Status</span>
        <span>Key</span>
        <span>Title</span>
        <span>Stage</span>
        <span className="text-right">Est.</span>
      </div>
      <div className="flex flex-col gap-4">
        {lanes.map((lane) => (
          <BoardLane key={lane.id} lane={lane} open={!folded.has(lane.id)} onToggle={() => onFold(lane.id)}>
            {(body) => <LaneRows lane={lane} now={now} openId={openId} onOpen={onOpen} body={body} />}
          </BoardLane>
        ))}
      </div>
    </div>
  );
}
