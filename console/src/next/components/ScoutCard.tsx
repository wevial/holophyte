import { formatClock } from "../../lib/format";
import { useAction } from "../actions";
import type { CardRow } from "../scoutThread";
import { Btn } from "./Btn";

/** A scout's proposal card: its question, a button per choice that posts the
 *  choice's label as the answer, and Type an answer; an answered card keeps
 *  only its head and question. */
export function ScoutCard({ base, scout, card, blocked, onType }: {
  base: string;
  scout: number;
  card: CardRow;
  blocked: string | null;
  onType: () => void;
}) {
  const action = useAction();
  const disabled = blocked != null || action.isPending;
  const choose = (choice: CardRow["choices"][number]) => {
    action.mutate({
      label: choice.mode === "ask" ? "Ask" : "Steer", base, route: "/actions/scout-message",
      body: { scout, mode: choice.mode, text: choice.label, answers: card.id },
      said: choice.mode === "ask" ? `Asked scout ${scout}.` : `Steered scout ${scout}.`,
    });
  };
  return (
    <li data-scout-card className="border border-l-[3px] border-act bg-paper px-3 py-2.5 font-read text-[14px] leading-[1.45] text-ink [overflow-wrap:anywhere]">
      <div className="mb-1.5 font-mono text-[11px] font-medium leading-[1.3] tracking-[.1em] text-act">
        <span>Scout · {formatClock(card.at)}</span>
      </div>
      <p className="m-0 whitespace-pre-line">{card.question}</p>
      {!card.answered && (
        <>
          <div className="mt-2.5 flex flex-wrap gap-2">
            {card.choices.map((choice, index) => (
              <Btn key={index} disabled={disabled} title={blocked ?? undefined} busy={action.isPending && action.variables?.body.text === choice.label} onClick={() => choose(choice)}>
                {choice.label}
              </Btn>
            ))}
          </div>
          <button type="button" disabled={disabled} title={blocked ?? undefined} onClick={onType}
            className="mt-2 font-mono text-[12px] text-ink-2 underline underline-offset-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40">
            Type an answer
          </button>
        </>
      )}
      {action.data?.ok === false && <p role="alert" className="m-0 mt-2 break-words font-mono text-[12px] leading-normal text-bad">{action.data.detail}</p>}
    </li>
  );
}
