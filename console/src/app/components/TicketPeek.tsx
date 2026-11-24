import { useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Dialog, DialogClose, DialogContent, DialogTitle } from "@ui-sdk/react/dialog";
import { criteriaRows } from "../../lib/criteria";
import { formatAge } from "../../lib/format";
import { summaryOf } from "../../lib/ticket";
import { useRun, useTicket } from "../queries";
import { keepScope, PollDepsContext, useConsolePeers } from "../scope";
import { STATES, type Tone } from "../state";
import { cardState, cardWord, COLUMN_HEADS, type ColumnId, type TicketCard } from "../tickets";
import { CardActions } from "./CardActions";
import { Criteria, SECTION_LABEL } from "./Criteria";
import { describeOn } from "./FocusCard";
import { stageWord } from "./StageChip";
import { StateDot, TONE_TEXT } from "./StateDot";
import { TitleText } from "./TitleText";

const HEAD_BUTTON =
  "inline-flex size-7 items-center justify-center rounded-[6px] text-ink-2 transition-colors duration-150 hover:bg-hover hover:text-ink motion-reduce:transition-none";

export function Chip({ tone, children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex w-fit items-center gap-1.5 whitespace-nowrap font-sans text-[13px] font-medium leading-[1.3] ${tone == null ? "text-ink" : TONE_TEXT[tone]}`}>
      {children}
    </span>
  );
}

function RunChip({ card, now }: { card: TicketCard; now: number }) {
  const state = cardState(card);
  if (state === "running" && card.run != null) {
    const { phase, round, max_rounds } = card.run;
    return (
      <Chip tone={STATES.running.tone}>
        <StateDot state="running" hideWord />
        <span>{stageWord({ phase, round: round + 1, max_rounds })}</span>
      </Chip>
    );
  }
  return (
    <Chip tone={STATES[state].tone}>
      <StateDot state={state} hideWord tone={STATES[state].tone} />
      <span>{cardWord(card, now)}</span>
    </Chip>
  );
}

export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="font-sans text-[13px] leading-[1.45] text-ink-3">{label}</dt>
      <dd className="m-0 min-w-0 font-sans text-[13px] leading-[1.45] [overflow-wrap:anywhere] text-ink">{children}</dd>
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
    return <p className="m-0 mt-5 font-sans text-[13px] leading-[1.45] text-ink-3">This host has not mirrored {card.key}.</p>;
  }
  return (
    <>
      <div className={`${SECTION_LABEL} mt-5`}>Summary</div>
      <p className="m-0 mt-2.5 max-w-[75ch] line-clamp-4 font-sans text-[14px] leading-[1.5] text-ink">{summaryOf(ticket.body)}</p>
      <div className="mt-[18px]">
        <Criteria rows={criteriaRows(ticket.criteria, run.data?.criteria ?? [], runId != null)} />
      </div>
    </>
  );
}

/** The card's Needs you ask and its primary action; once the action is
 *  taken, the sentence the toast said until another ask stands. */
function Ask({ card, now }: { card: TicketCard; now: number }) {
  const { hosts } = useConsolePeers();
  const [acted, setActed] = useState<{ id: string; ask: string | null; said: string } | null>(null);
  const ask = card.need == null ? null : `${card.need.item.kind} ${String(card.need.item.run)}`;
  if (acted?.id === card.id && (ask == null || ask === acted.ask)) {
    return (
      <p data-peek-acted className="m-0 mt-4 flex items-center gap-2 font-mono text-[12.5px] font-medium leading-[1.4] text-live">
        <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M3.5 8.4 6.6 11.5 12.5 5" />
        </svg>
        {acted.said}
      </p>
    );
  }
  if (card.need == null) return null;
  const host = hosts.find((candidate) => candidate.base === card.base);
  const description = describeOn(card.need.item, host, now);
  return (
    <div data-peek-ask className="mt-4 rounded-[10px] bg-raised px-3.5 py-3 shadow-[inset_0_0_0_1px_var(--need-edge)]">
      <h3 className="m-0 font-sans text-[13px] font-semibold leading-[1.3] text-need">Needs you</h3>
      <p className="m-0 mb-3 mt-1.5 max-w-[75ch] break-words font-sans text-[14px] leading-[1.5] text-ink">{description.body}</p>
      <CardActions
        key={`${card.id} ${ask}`}
        entry={card.need}
        host={host}
        actions={description.actions}
        title={card.title}
        only="primary"
        onActed={(said) => setActed({ id: card.id, ask, said })}
      />
    </div>
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
  const close = useRef<HTMLButtonElement>(null);
  const previousCard = useRef(card.id);
  useEffect(() => {
    if (previousCard.current === card.id) return;
    previousCard.current = card.id;
    close.current?.focus();
  }, [card.id]);
  const head = COLUMN_HEADS[column];
  return (
    <Dialog
      open
      modal={false}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent
        data-peek=""
        initialFocus={() => close.current}
        finalFocus="none"
        className="fixed inset-y-0 right-0 z-40 box-border flex w-[min(420px,100vw)] flex-col rounded-l-[12px] bg-raised text-ink outline-none shadow-[0_0_0_1px_var(--hair-2),0_10px_28px_light-dark(rgba(29,24,43,.12),rgba(0,0,0,.35))] max-[720px]:top-[30px] max-[720px]:w-full max-[720px]:rounded-none"
      >
        <div className="flex items-center gap-1 pl-[18px] pr-[18px] pt-4">
          <span data-peek-key className="mr-auto font-mono text-[12px] leading-none tabular-nums text-ink-3">{card.key}</span>
          <span className="flex gap-1">
            <Link
              to="/tickets/$project/$key"
              params={{ project: card.project, key: card.key }}
              search={keepScope}
              aria-label="Expand to the full ticket page"
              className={HEAD_BUTTON}
            >
              <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9" />
              </svg>
            </Link>
            <DialogClose ref={close} aria-label="Close preview" className={HEAD_BUTTON}>
              <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" aria-hidden="true">
                <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />
              </svg>
            </DialogClose>
          </span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-[18px] pb-7 pt-3">
          <DialogTitle className="m-0 break-words font-sans text-[17px] font-semibold leading-[1.3] tracking-[-0.01em]">
            <TitleText>{card.title}</TitleText>
          </DialogTitle>
          <div data-peek-column className="mt-3">
            <Chip tone={column === "needs_you" ? "need" : STATES[head.state].tone}>
              <StateDot state={head.state} hideWord tone={column === "needs_you" ? "need" : undefined} />
              <span>{head.label}</span>
            </Chip>
          </div>
          <Ask card={card} now={now} />
          <dl className="mt-4 grid grid-cols-[90px_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1.5">
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
      </DialogContent>
    </Dialog>
  );
}
