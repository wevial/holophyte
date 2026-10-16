import { useContext, useEffect, useId, useRef, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { criteriaRows } from "../../lib/criteria";
import { formatAge } from "../../lib/format";
import { summaryOf } from "../../lib/ticket";
import { useRun, useTicket } from "../queries";
import { keepScope, PollDepsContext } from "../scope";
import { STATES } from "../state";
import { cardState, cardWord, COLUMN_HEADS, type ColumnId, type TicketCard } from "../tickets";
import { Criteria, SECTION_LABEL } from "./Criteria";
import { StageChip } from "./StageChip";
import { StateDot } from "./StateDot";

const HEAD_BUTTON =
  "inline-flex min-h-10 min-w-10 items-center justify-center gap-1.5 border border-rule px-2.5 font-mono text-[11px] font-medium uppercase leading-none tracking-[.1em] text-ink-2 hover:border-ink-3 hover:text-ink";

export function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex min-h-[30px] w-fit items-center gap-2 whitespace-nowrap border border-rule bg-paper-2 px-2.5 font-mono text-[12px] font-medium leading-none text-ink">
      {children}
    </span>
  );
}

function RunChip({ card, now }: { card: TicketCard; now: number }) {
  const state = cardState(card);
  if (state === "running" && card.run != null) {
    const { phase, round, max_rounds } = card.run;
    return <StageChip run={{ phase, round: round + 1, max_rounds }} />;
  }
  return (
    <Chip>
      <StateDot state={state} hideWord tone={STATES[state].tone} />
      <span>{cardWord(card, now)}</span>
    </Chip>
  );
}

export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="font-sans text-[12px] leading-[1.4] tracking-[.01em] text-ink-3">{label}</dt>
      <dd className="m-0 min-w-0 font-sans text-[14px] font-medium leading-[1.4] [overflow-wrap:anywhere] text-ink">{children}</dd>
    </>
  );
}

export const NUMBER = "font-mono text-[13px] font-medium tabular-nums";

/** Blank while `/tickets/KEY` loads; the run's standings fill in as they land. */
function Contract({ card }: { card: TicketCard }) {
  const deps = useContext(PollDepsContext);
  const { data: ticket, isSuccess } = useTicket(card.base, card.key, deps);
  const runId = ticket?.run ?? null;
  const run = useRun(runId == null ? null : card.base, runId, deps);
  if (!isSuccess) return null;
  if (ticket == null) {
    return <p className="mt-5 font-mono text-[13px] text-ink-3">This host has not mirrored {card.key}.</p>;
  }
  return (
    <>
      <div className={`${SECTION_LABEL} mt-5`}>Summary</div>
      <p className="m-0 mt-2.5 line-clamp-4 font-sans text-[14px] leading-[1.5] text-ink">{summaryOf(ticket.body)}</p>
      <div className="mt-[18px]">
        <Criteria rows={criteriaRows(ticket.acceptance_criteria, run.data?.criteria ?? [], runId != null)} />
      </div>
    </>
  );
}

/** A non-modal panel docked right over a live board: Tab is not trapped. */
export function TicketPeek({
  card,
  column,
  now,
  onClose,
}: {
  card: TicketCard;
  column: ColumnId;
  now: number;
  onClose: () => void;
}) {
  const titleId = useId();
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    close.current?.focus();
  }, [card.id]);
  const head = COLUMN_HEADS[column];
  return (
    <section
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      className="fixed inset-y-0 right-0 z-40 box-border flex w-[min(420px,100vw)] flex-col border-l border-rule bg-paper text-ink shadow-[-8px_0_24px_rgba(0,0,0,.28)] max-[720px]:w-full max-[720px]:border-l-0"
    >
      <div className="flex min-h-14 items-center gap-2 border-b border-rule-2 pl-5 pr-2.5">
        <span data-peek-key className="font-mono text-[12px] font-medium leading-none tabular-nums text-ink-3">{card.key}</span>
        <span className="ml-auto flex gap-1.5">
          <Link to="/tickets/$project/$key" params={{ project: card.project, key: card.key }} search={keepScope} className={HEAD_BUTTON}>
            <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9" />
            </svg>
            Expand<span className="sr-only"> to the full ticket page</span>
          </Link>
          <button ref={close} type="button" aria-label="Close preview" onClick={onClose} className={HEAD_BUTTON}>
            <svg viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" aria-hidden="true">
              <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />
            </svg>
          </button>
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-7 pt-[18px]">
        <h2 id={titleId} className="break-words font-sans text-[22px] font-extrabold leading-[1.15] tracking-[-0.01em] [font-stretch:108%]">
          {card.title}
        </h2>
        <div data-peek-column className="mt-3">
          <Chip>
            <StateDot state={head.state} hideWord tone={column === "needs_you" ? "need" : undefined} />
            <span>{head.label}</span>
          </Chip>
        </div>
        <dl className="mt-4 grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-x-3 gap-y-2.5">
          <Fact label="Project">{card.project}</Fact>
          <Fact label="Time box">{card.timeBoxMs == null ? "none" : <span className={NUMBER}>{formatAge(card.timeBoxMs)}</span>}</Fact>
          <Fact label="Run">
            {card.runId == null ? (
              "none yet"
            ) : (
              <>
                <span className={NUMBER}>run {card.runId}</span>
                <span data-peek-run-state className="mt-1.5 flex">
                  <RunChip card={card} now={now} />
                </span>
              </>
            )}
          </Fact>
        </dl>
        <Contract key={card.id} card={card} />
      </div>
    </section>
  );
}
