import { Link } from "@tanstack/react-router";
import { formatAge } from "../../lib/format";
import { projectColours } from "../dayLog";
import { STATES, type Tone } from "../state";
import { cardState, cardWord, COLUMN_HEADS, type ColumnId, type Lane, type TicketCard } from "../tickets";
import { ACTIONS_OFF } from "../../lib/actions";
import { useContext, useState, type ReactNode } from "react";
import { useTicket } from "../queries";
import { keepScope, PollDepsContext, useConsolePeers } from "../scope";
import { cardElementId, modified, TicketGlyph } from "./BoardCard";
import { BoardLane, type LaneBodyProps } from "./BoardLane";
import { TONE_TEXT } from "./StateDot";
import { TitleText } from "./TitleText";
import { MoreMenu, ROW_MENU, type MenuItem } from "./MoreMenu";
import { usePlaceItems, useRevertFlow } from "./Revert";

const ROW_ORDER: ColumnId[] = ["needs_you", "in_flight", "ready", "backlog", "done"];

const byKey = (a: TicketCard, b: TicketCard) => a.key.localeCompare(b.key, undefined, { numeric: true });

const GRID_FRAME = "grid items-center gap-4 pl-4 pr-14 max-[820px]:grid-cols-[auto_auto_minmax(0,1fr)]";
const GRID = `${GRID_FRAME} grid-cols-[132px_96px_minmax(0,1fr)_220px_40px]`;
const GLYPH_GRID = `${GRID_FRAME} grid-cols-[auto_96px_minmax(0,1fr)_220px_40px]`;

const columnTone = (column: ColumnId): Tone => (column === "needs_you" ? "need" : STATES[COLUMN_HEADS[column].state].tone);

/** With its ⋯ menu open a Done row shows its glyph alone, its key close
 *  beside it, names its project
 *  beside the title and ends on its muted merge time, its estimate dropped. */
function TicketRow({ card, column, now, open, menuOpen, onOpen }: {
  card: TicketCard; column: ColumnId; now: number; open: boolean; menuOpen: boolean; onOpen: (card: TicketCard) => void;
}) {
  const tone = columnTone(column);
  const quiet = column === "backlog" || column === "done";
  const merged = menuOpen && column === "done";
  const colour = projectColours(useConsolePeers().hosts).get(card.project);
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
      className={`${merged ? GLYPH_GRID : GRID} min-h-[42px] py-2 [:root[data-density=compact]_&]:min-h-[34px]! [:root[data-density=compact]_&]:py-1.5! font-sans text-[13px] leading-[1.3] text-ink-2 no-underline transition-colors duration-(--motion-colour) ${open ? "bg-selected" : "hover:bg-[color-mix(in_srgb,var(--hover)_50%,var(--raised))]"}`}
    >
      <span data-cell="status" className={`inline-flex items-center gap-[5px] whitespace-nowrap text-[12.5px] ${merged ? "-mr-2 max-[820px]:mr-0" : ""} ${open ? "text-ink" : TONE_TEXT[tone]}`}>
        <TicketGlyph state={COLUMN_HEADS[column].state} tone={tone} />
        <span className={merged ? "sr-only" : undefined}>{COLUMN_HEADS[column].label}</span>
      </span>
      <span data-cell="key" className={`font-mono text-[12px] tabular-nums ${open ? "text-ink-2" : "text-ink-3"}`}>{card.key}</span>
      <span
        data-cell="title"
        className={`flex min-w-0 items-center gap-4 font-medium leading-[1.35] max-[820px]:col-span-full max-[820px]:row-start-2 max-[820px]:whitespace-normal ${quiet && !open ? "text-ink-2 group-has-[[aria-expanded=true]]:text-ink" : "text-ink"}`}
      >
        <span className="min-w-0 truncate max-[820px]:whitespace-normal">
          <TitleText>{card.title}</TitleText>
        </span>
        {merged && (
          <span data-cell="project" className="ml-auto inline-flex shrink-0 items-center gap-2 font-normal text-ink-2">
            <span aria-hidden="true" className="size-1.5 rounded-full" style={{ background: colour ?? "var(--ink-3)" }} />
            {card.project}
          </span>
        )}
      </span>
      <span data-cell="stage" className={`truncate text-[12.5px] ${merged ? "col-span-2 text-right text-ink-3 max-[820px]:col-span-1" : `max-[820px]:hidden ${open ? "text-ink" : TONE_TEXT[STATES[cardState(card)].tone]}`}`}>{cardWord(card, now)}</span>
      {!merged && (
        <span data-cell="estimate" className={`text-right font-mono text-[12px] tabular-nums max-[820px]:hidden ${open ? "text-ink-2" : "text-ink-3"}`}>
          {card.timeBoxMs != null && (
            <>
              {formatAge(card.timeBoxMs)}
              <span className="sr-only"> time box</span>
            </>
          )}
        </span>
      )}
    </Link>
  );
}

/** A row's ⋯ menu where its project reports `reverts`: its ticket is read
 *  while the menu is open, whatever column the row sits in. Revert… stays
 *  disabled while that read is fetching or has failed, even over an earlier
 *  answer; any row offers it once the read finds its newest run merged. A reverted ticket's Revert… is disabled naming
 *  the revert ticket; a ticket whose newest run did not merge offers none. */
function MergedRowMenu({ card, column, actions, items, onOpenChange }: {
  card: TicketCard; column: ColumnId; actions: boolean; items: MenuItem[]; onOpenChange: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const ticket = useTicket(card.base, card.key, useContext(PollDepsContext), open);
  const read = ticket.data;
  const newest = read?.runs.reduce<number>((top, each) => Math.max(top, each.run), -1);
  const merged = read == null ? column === "done" : read.runs.find((each) => each.run === newest)?.outcome === "merged";
  const revert = useRevertFlow(
    merged ? { base: card.base, project: card.project, ticket: card.key, run: newest ?? card.runId, newest: true, reverted: read?.reverted, off: actions ? null : ACTIONS_OFF } : null,
    items,
  );
  const unread = ticket.isPending || ticket.isFetching ? "Checking…" : ticket.isError || read == null ? `Couldn't read ${card.key}` : null;
  const shown = revert.items.map((item) => (unread != null && item.label === "Revert…" ? { ...item, disabled: true, note: unread } : item));
  return <RowMenuFrame card={card} items={shown} layer={revert.layer} onOpenChange={(now) => {
    setOpen(now);
    onOpenChange(now);
  }} />;
}

function RowMenuFrame({ card, items, layer, onOpenChange }: { card: TicketCard; items: MenuItem[]; layer?: ReactNode; onOpenChange?: (open: boolean) => void }) {
  return (
    <>
      <MoreMenu compact label={`More for ${card.key}`} items={items} onOpenChange={onOpenChange} className={`${ROW_MENU} absolute top-2 right-3 [:root[data-density=compact]_&]:top-1`} />
      <div className="px-4 pb-2 empty:hidden">{layer}</div>
    </>
  );
}

/** A row's ⋯ menu: Open ticket, Copy link, and in a project that reports
 *  `reverts`, Revert… once its ticket's newest run merged; a filed revert's
 *  status line reads under the row. */
function TicketRowMenu({ card, column, onOpenChange }: { card: TicketCard; column: ColumnId; onOpenChange: (open: boolean) => void }) {
  const status = useConsolePeers().hosts.find((each) => each.base === card.base)?.status;
  const place = { project: card.project, ticket: card.key };
  const items = usePlaceItems(place, place);
  if (status?.reverts === true) {
    return <MergedRowMenu card={card} column={column} actions={status.actions === true} items={items} onOpenChange={onOpenChange} />;
  }
  return <RowMenuFrame card={card} items={items} onOpenChange={onOpenChange} />;
}

function TicketItem({ card, column, now, open, onOpen }: { card: TicketCard; column: ColumnId; now: number; open: boolean; onOpen: (card: TicketCard) => void }) {
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <li className="group relative border-b border-hair last:border-b-0 has-[[aria-expanded=true]]:bg-[color-mix(in_srgb,var(--selected)_45%,transparent)]">
      <TicketRow card={card} column={column} now={now} open={open} menuOpen={menuOpen} onOpen={onOpen} />
      <TicketRowMenu card={card} column={column} onOpenChange={setMenuOpen} />
    </li>
  );
}

function LaneRows({ lane, now, openId, onOpen, body }: LaneBodyProps) {
  return (
    <ul id={body.id} hidden={body.hidden} data-lane-rows className={body.flat ? "" : "pb-1.5"}>
      {ROW_ORDER.flatMap((column) =>
        lane.columns[column].toSorted(byKey).map((card) => <TicketItem key={card.id} card={card} column={column} now={now} open={card.id === openId} onOpen={onOpen} />),
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
      <div data-table-heads className={`${GRID} min-h-[30px] border-b border-hair text-[12px] font-medium leading-none text-ink-3 max-[820px]:hidden`}>
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
