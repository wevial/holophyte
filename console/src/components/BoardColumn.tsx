import type { Column } from "../lib/board";
import type { BoardState } from "../lib/types";
import { TicketCard } from "./TicketCard";

/** The header dot per state: grey, amber, ok green, red, teal. */
const DOTS: Record<BoardState, string> = {
  needs_spec: "bg-faint",
  blocked_on_deps: "bg-warn",
  ready: "bg-ok",
  blocked_on_operator: "bg-bad",
  in_flight: "bg-accent",
};

/** One column of the Board: dot, label and count over the well of cards. */
export function BoardColumn({ column }: { column: Column }) {
  return (
    <section aria-label={column.label} data-column={column.state} className="flex min-w-0 flex-col gap-2">
      <header className="flex items-center gap-2 px-1">
        <span aria-hidden="true" className={`size-2 rounded-full ${DOTS[column.state]}`} />
        <span className="text-[12px] font-semibold text-ink">{column.label}</span>
        <span data-count className="font-mono text-[11px] text-faint">
          {column.count}
        </span>
      </header>
      <div className="flex min-h-[120px] flex-col gap-2 rounded-[10px] bg-well p-2">
        {column.cards.map((card) => (
          <TicketCard key={card.key} card={card} />
        ))}
      </div>
    </section>
  );
}
