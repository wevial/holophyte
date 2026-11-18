import type { MouseEvent } from "react";
import { Link } from "@tanstack/react-router";
import { formatAge } from "../../lib/format";
import { STATES, type Tone } from "../state";
import { cardState, cardWord, type ColumnId, type TicketCard } from "../tickets";
import { keepScope } from "../scope";
import { StateDot, TONE_TEXT } from "./StateDot";
import { TitleText } from "./TitleText";

type Look = "need" | "done" | "backlog" | "plain";

const LOOK_OF: Record<ColumnId, Look> = { needs_you: "need", done: "done", backlog: "backlog", ready: "plain", in_flight: "plain" };

const LOOKS: Record<Look, { card: string; title: string }> = {
  need: { card: "bg-raised shadow-[inset_0_0_0_1px_var(--need-edge)] hover:bg-hover forced-colors:border", title: "text-ink" },
  done: { card: "bg-raised shadow-[inset_0_0_0_1px_var(--hair)] hover:bg-hover forced-colors:border", title: "text-ink-2" },
  backlog: { card: "border border-dashed border-hair-2 bg-transparent hover:bg-hover", title: "text-ink-3" },
  plain: { card: "bg-raised shadow-[inset_0_0_0_1px_var(--hair)] hover:bg-hover forced-colors:border", title: "text-ink" },
};

const OPEN_RING = "shadow-[inset_0_0_0_1px_var(--accent),0_0_0_1px_var(--accent)]! forced-colors:outline-2 forced-colors:outline-solid forced-colors:outline-[Highlight]";

export const cardElementId = (card: Pick<TicketCard, "project" | "key">) => `card-${card.project}-${card.key}`;

export const modified = (event: MouseEvent) => event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;

export function BoardCard({
  card,
  column,
  now,
  open,
  onOpen,
}: {
  card: TicketCard;
  column: ColumnId;
  now: number;
  open: boolean;
  onOpen: (card: TicketCard) => void;
}) {
  const look = LOOK_OF[column];
  const state = cardState(card);
  const tone: Tone = look === "backlog" ? "ink-3" : STATES[state].tone;
  const wordTone: Tone = look === "done" ? "ink-3" : tone;
  return (
    <Link
      id={cardElementId(card)}
      to="/tickets/$project/$key"
      params={{ project: card.project, key: card.key }}
      search={keepScope}
      aria-haspopup="dialog"
      data-open={open || undefined}
      onClick={(event) => {
        if (modified(event) || event.button !== 0) return;
        event.preventDefault();
        onOpen(card);
      }}
      className="block text-inherit no-underline"
    >
      <article
        data-look={look}
        className={`relative flex flex-col gap-2 rounded-[10px] px-3.5 py-3 transition-colors duration-(--motion-colour) ${LOOKS[look].card} ${open ? OPEN_RING : ""}`}
      >
        <p className="font-mono text-[12px] leading-[1.45] text-ink-3 tabular-nums">
          <span>{card.key}</span>
        </p>
        <h3 className={`break-words font-sans text-[13.5px] font-medium leading-[1.35] ${LOOKS[look].title}`}><TitleText>{card.title}</TitleText></h3>
        <div className="flex min-w-0 items-center gap-2 text-[12px] leading-[1.45]">
          <p data-state-line className="flex min-w-0 items-center gap-[5px]">
            <StateDot state={state} hideWord tone={tone} />
            <span className={`min-w-0 [overflow-wrap:anywhere] ${TONE_TEXT[wordTone]}`}>{cardWord(card, now)}</span>
          </p>
          {card.timeBoxMs != null && (
            <span data-time-box className="ml-auto shrink-0 font-mono text-ink-3 tabular-nums">
              {formatAge(card.timeBoxMs)}
              <span className="sr-only"> time box</span>
            </span>
          )}
        </div>
      </article>
    </Link>
  );
}
