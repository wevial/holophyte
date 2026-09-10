import { cardLine, type BoardCard } from "../lib/board";
import { ActionButton } from "./ActionButton";
import { KindPill } from "./KindPill";
import { PhasePill } from "./PhasePill";
import { StrikePill } from "./StrikePill";
import { BoxBar } from "./TimeBoxBar";

/** One open ticket on the Board: ticket, its pills, the project at the
 *  right; the title; for an in-progress card the 5px time-box bar; then
 *  the state's sub-line (`cardLine`). The phase pill, strike pill and
 *  bar are the Floor's. The identifier is a button that opens the
 *  ticket's sheet (`onOpen`), pressed while `open`. The two actions
 *  render disabled, tooltip `WRITES_LATER`, until the write ticket
 *  lands. */
export function TicketCard({ card, open = false, onOpen }: { card: BoardCard; open?: boolean; onOpen?: (card: BoardCard) => void }) {
  const line = cardLine(card);
  const { run } = card;
  return (
    <article
      data-ticket={card.ticket}
      data-card-key={card.key}
      data-state={card.status}
      className="flex flex-col gap-[6px] rounded-[8px] border border-line bg-card px-3 py-[10px] shadow-card"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-[6px]">
        <button
          type="button"
          data-open-ticket
          aria-pressed={open}
          onClick={() => onOpen?.(card)}
          className="rounded-[4px] font-mono text-[12px] font-semibold text-ink underline-offset-2 hover:underline"
        >
          {card.ticket}
        </button>
        {card.status === "blocked_on_operator" && <KindPill kind="blocked">question</KindPill>}
        {card.status === "in_flight" && run && <PhasePill phase={run.phase} />}
        {card.status === "in_flight" && run && <StrikePill strikes={run.strikes ?? 0} max={card.strikesMax} />}
        <span className="ml-auto truncate text-[11px] text-faint">{card.project}</span>
      </div>
      <p className="text-[13px] leading-[1.4] text-ink">{card.title ?? ""}</p>
      {card.status === "in_flight" && run && <BoxBar elapsedMs={run.elapsed_ms} boxMs={run.time_box_ms} height={5} />}
      {line && (
        <p data-line className="font-mono text-[11px] text-faint">
          {line}
        </p>
      )}
      <div data-actions className="flex gap-2">
        <ActionButton>Edit ticket</ActionButton>
        <ActionButton>Mark needs_spec</ActionButton>
      </div>
    </article>
  );
}
