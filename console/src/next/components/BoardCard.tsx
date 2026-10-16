import { formatAge } from "../../lib/format";
import { STATES, type Tone } from "../state";
import { cardState, cardWord, type ColumnId, type TicketCard } from "../tickets";
import { StateDot, TONE_TEXT } from "./StateDot";

type Look = "need" | "done" | "backlog" | "plain";

const LOOK_OF: Record<ColumnId, Look> = { needs_you: "need", done: "done", backlog: "backlog", ready: "plain", in_flight: "plain" };

const LOOKS: Record<Look, { card: string; title: string }> = {
  need: { card: "border-[color-mix(in_srgb,var(--need)_45%,var(--rule))] bg-card", title: "text-ink" },
  done: { card: "border-rule bg-live-wash", title: "text-ink-2" },
  backlog: { card: "border-dashed border-rule bg-transparent", title: "text-ink-3" },
  plain: { card: "border-rule bg-card", title: "text-ink" },
};

export function BoardCard({ card, column, now }: { card: TicketCard; column: ColumnId; now: number }) {
  const look = LOOK_OF[column];
  const state = cardState(card);
  const tone: Tone = look === "backlog" ? "ink-3" : STATES[state].tone;
  const wordTone: Tone = look === "done" ? "ink-3" : tone;
  return (
    <article data-look={look} className={`relative flex flex-col border px-4 pb-[15px] pt-3.5 ${LOOKS[look].card}`}>
      <div className="flex items-center gap-2 font-mono text-[11px] font-medium leading-none text-ink-3 tabular-nums">
        <span>{card.key}</span>
        {card.timeBoxMs != null && (
          <span data-time-box className="ml-auto inline-grid h-5 min-w-5 place-items-center px-1 text-ink-2 shadow-[inset_0_0_0_1px_var(--rule)]">
            <span>
              {formatAge(card.timeBoxMs)}
              <span className="sr-only"> time box</span>
            </span>
          </span>
        )}
      </div>
      <h3 className={`mt-[9px] break-words font-sans text-[14px] font-medium leading-[1.45] ${LOOKS[look].title}`}>{card.title}</h3>
      <p data-state-line className="mt-3 flex min-w-0 items-center gap-[7px] font-mono text-[12px] leading-[1.3]">
        <StateDot state={state} hideWord tone={tone} />
        <span className={`min-w-0 [overflow-wrap:anywhere] ${TONE_TEXT[wordTone]}`}>{cardWord(card, now)}</span>
      </p>
    </article>
  );
}
