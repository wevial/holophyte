import { useEffect, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { cardElementId } from "../components/BoardCard";
import { BoardLane, GRID, LaneGrid } from "../components/BoardLane";
import { GroupBy, readGroup, writeGroup } from "../components/GroupBy";
import { StateDot } from "../components/StateDot";
import { TicketPeek } from "../components/TicketPeek";
import { TicketTable } from "../components/TicketTable";
import { readView, ViewToggle, writeView, type TicketView } from "../components/ViewToggle";
import { storeScope } from "../projects";
import type { ScopeSearch } from "../scope";
import {
  boardLanes,
  COLUMN_HEADS,
  COLUMN_IDS,
  FILTER_LABELS,
  ticketView,
  useTicketBoard,
  type ColumnId,
  type GroupMode,
  type TicketCard,
  type TicketsSearch,
} from "../tickets";

function Slash() {
  return <span className="text-ink-3"> / </span>;
}

function findCard(columns: Record<ColumnId, TicketCard[]>, id: string | null): { card: TicketCard; column: ColumnId } | null {
  for (const column of COLUMN_IDS) {
    const card = columns[column].find((each) => each.id === id);
    if (card != null) return { card, column };
  }
  return null;
}

export function Tickets() {
  const board = useTicketBoard();
  const { filter }: TicketsSearch = useSearch({ from: "/tickets" });
  const shown = (cards: TicketCard[]) => (filter == null ? cards : cards.filter((card) => ticketView(card) === filter));
  const columns = Object.fromEntries(COLUMN_IDS.map((id) => [id, shown(board.columns[id])])) as Record<ColumnId, TicketCard[]>;
  const cards = shown(board.cards);
  const view = { ...board, columns, cards, total: cards.length };
  const navigate = useNavigate();
  const [openId, setOpenId] = useState<string | null>(null);
  const [layout, setLayout] = useState<TicketView>(readView);
  const [group, setGroup] = useState<GroupMode>(readGroup);
  const [folded, setFolded] = useState<ReadonlySet<string>>(new Set());
  const modes: GroupMode[] = view.project == null ? ["project", "story", "none"] : ["story", "none"];
  const mode = modes.includes(group) ? group : "none";
  const lanes = boardLanes(view.cards, mode, view.boards);
  const anyOpen = lanes.some((lane) => !folded.has(lane.id));
  const pickGroup = (next: GroupMode) => {
    writeGroup(next);
    setGroup(next);
  };
  const pickView = (next: TicketView) => {
    writeView(next);
    setLayout(next);
  };
  const foldLane = (id: string) => {
    const next = new Set(folded);
    if (!next.delete(id)) next.add(id);
    setFolded(next);
  };
  const peek = findCard(columns, openId);
  const opener = peek == null ? null : cardElementId(peek.card);
  useEffect(() => {
    if (opener == null) return;
    const shut = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      setOpenId(null);
      document.getElementById(opener)?.focus();
    };
    document.addEventListener("keydown", shut);
    return () => document.removeEventListener("keydown", shut);
  }, [opener]);
  const closePeek = () => {
    setOpenId(null);
    if (opener != null) document.getElementById(opener)?.focus();
  };
  const empty = (
    <p className="mt-4 font-mono text-[13px] text-ink-3">{view.project == null ? "No open tickets." : `No open tickets in ${view.project}.`}</p>
  );
  return (
    <section aria-labelledby="page-title" className="flex flex-col px-8 py-7 min-[720px]:h-full">
      <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Tickets</h1>
        {view.answered && (
          <p data-count-line className="font-mono text-[13px] [overflow-wrap:anywhere] text-ink-2">
            {view.project ?? "all projects"} · {view.total} {view.total === 1 ? "ticket" : "tickets"}
            <Slash />
            <b className="font-semibold text-ink">{columns.in_flight.length}</b> in flight
            <Slash />
            <b className="font-semibold text-need">{columns.needs_you.length}</b> need you
            <Slash />
            <b className="font-semibold text-ink">{columns.ready.length}</b> ready
          </p>
        )}
        {view.project != null && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3.5 gap-y-2 font-mono text-[12.5px] text-ink-2">
            <span>
              Showing <b className="font-medium [overflow-wrap:anywhere] text-act">{view.project}</b> only
            </span>
            <button
              type="button"
              onClick={() => {
                storeScope(null);
                void navigate({ to: ".", search: (previous: ScopeSearch) => ({ ...previous, project: undefined }) });
              }}
              className="inline-flex min-h-8 cursor-pointer items-center font-medium text-act underline underline-offset-2"
            >
              Show all projects
            </button>
          </div>
        )}
        {filter != null && (
          <div className="flex flex-wrap items-center gap-x-3.5 gap-y-2 font-mono text-[12.5px] text-ink-2">
            <span>
              Showing <b className="font-medium text-act">{FILTER_LABELS[filter]}</b> tickets only
            </span>
            <button
              type="button"
              onClick={() => void navigate({ to: ".", search: (previous: ScopeSearch & TicketsSearch) => ({ ...previous, filter: undefined }) })}
              className="inline-flex min-h-8 cursor-pointer items-center font-medium text-act underline underline-offset-2"
            >
              Show all tickets
            </button>
          </div>
        )}
      </header>
      {view.answered && (
        <div className="mt-6 flex flex-wrap items-center gap-x-4 gap-y-2.5">
          <ViewToggle view={layout} onPick={pickView} />
          <GroupBy modes={modes} mode={mode} onPick={pickGroup} />
          {mode !== "none" && (
            <button
              type="button"
              onClick={() => setFolded(anyOpen ? new Set(lanes.map((lane) => lane.id)) : new Set())}
              className="inline-flex min-h-8 cursor-pointer items-center font-mono text-[12.5px] font-medium text-act underline underline-offset-2"
            >
              {anyOpen ? "Collapse all lanes" : "Expand all lanes"}
            </button>
          )}
        </div>
      )}
      {view.answered && layout === "table" && (
        <div data-table-scroll className="mt-3.5 min-h-0 flex-1 overflow-auto pb-2">
          {view.total === 0 ? (
            empty
          ) : (
            <TicketTable lanes={lanes} now={view.now} openId={openId} onOpen={(next) => setOpenId(next.id)} folded={folded} onFold={foldLane} />
          )}
        </div>
      )}
      {view.answered && layout === "board" && (
        <div data-board-scroll className="mt-3.5 min-h-0 flex-1 overflow-auto pb-2">
          <div className="min-w-[1160px]">
            <div data-column-heads className={`${GRID} sticky top-0 z-10 bg-canvas ${mode === "none" ? "px-3" : "px-[17px]"}`}>
              {COLUMN_IDS.map((id) => (
                <div key={id} className="flex min-h-11 items-center gap-[9px] px-0.5 font-sans text-[13px] font-medium text-ink">
                  <StateDot state={COLUMN_HEADS[id].state} hideWord tone={id === "needs_you" ? "need" : undefined} />
                  <span>{COLUMN_HEADS[id].label}</span>
                  <b data-count className={`font-mono text-[12px] font-normal ${id === "needs_you" ? "text-need" : "text-ink-3"}`}>
                    {columns[id].length}
                  </b>
                </div>
              ))}
            </div>
            {view.total === 0 ? (
              empty
            ) : (
              <div className="mt-2 flex flex-col gap-4">
                {lanes.map((lane) => (
                  <BoardLane key={lane.id} lane={lane} open={!folded.has(lane.id)} onToggle={() => foldLane(lane.id)}>
                    {(body) => <LaneGrid lane={lane} now={view.now} openId={openId} onOpen={(next) => setOpenId(next.id)} body={body} />}
                  </BoardLane>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      {view.answered && (
        <p className="mt-3 font-mono text-[12px] text-ink-3">
          Cards move as the factory and you act. Click a card to preview it; Expand opens the full ticket.
        </p>
      )}
      {peek != null && <TicketPeek card={peek.card} column={peek.column} now={view.now} onClose={closePeek} />}
    </section>
  );
}
