import { useIsMutating } from "@tanstack/react-query";
import { useCallback, useId, useRef, useState, type Ref } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { useAction } from "../actions";
import { composeBlock, threadRows, type CardRow, type LineTone } from "../scoutThread";
import { Btn } from "./Btn";
import { ScoutCard } from "./ScoutCard";
import { Segmented } from "./Segmented";
import { ThreadMessage } from "./ThreadMessage";

const LINE_TONES: Record<LineTone, string> = { quiet: "text-ink-3", bad: "text-bad", wait: "text-wait" };

type Mode = "ask" | "steer";
const MODES: { value: Mode; label: string }[] = [{ value: "ask", label: "Ask" }, { value: "steer", label: "Steer" }];

export const SCOUT_HEADING = "m-0 flex h-[30px] items-center gap-2.5 border-b border-hair font-sans text-[13px] font-semibold text-ink";

export function CloseGlyph() {
  return (
    <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" aria-hidden="true">
      <path d="M4.5 4.5l7 7M11.5 4.5l-7 7" />
    </svg>
  );
}

const MESSAGE_ROUTE = "/actions/scout-message";

/** Whether any of the thread's posts to `base`, the composer's or a card's, is still in flight. */
const useSending = (base: string) => useIsMutating({
  predicate: (mutation) => {
    const post = mutation.state.variables as { base?: string; route?: string } | undefined;
    return post?.base === base && post.route === MESSAGE_ROUTE;
  },
}) > 0;

function ScoutCompose({ base, detail, actions, sending, answering, onStopAnswering, ref }: {
  base: string;
  detail: ScoutDetail;
  actions: boolean;
  sending: boolean;
  answering: CardRow | null;
  onStopAnswering: () => void;
  ref?: Ref<HTMLTextAreaElement>;
}) {
  const [mode, setMode] = useState<Mode>("ask");
  const [text, setText] = useState("");
  const action = useAction();
  const scout = detail.scout.id;
  const blocked = composeBlock(detail, actions);
  const disabled = blocked != null || sending;
  const asking = mode === "ask";
  const send = async () => {
    if (disabled || !text.trim()) return;
    const result = await action.mutateAsync({
      label: asking ? "Ask" : "Steer", base, route: MESSAGE_ROUTE,
      body: { scout, mode, text: text.trim(), ...(answering ? { answers: answering.id } : {}) },
      said: asking ? `Asked scout ${scout}.` : `Steered scout ${scout}.`,
    });
    if (!result.ok) return;
    setText("");
    onStopAnswering();
  };
  return (
    <div className="mt-4">
      <Segmented label="Message kind" options={MODES} value={mode} disabled={disabled} title={blocked ?? undefined}
        onPick={(picked) => { setMode(picked); action.reset(); }} />
      {answering && (
        <div data-answering className="mt-2.5 flex items-start justify-between gap-2 font-sans text-[13px] leading-[1.4] text-ink-2 [overflow-wrap:anywhere]">
          <span className="pt-1">Answering: {answering.question}</span>
          <button type="button" aria-label="Stop answering" title="Stop answering" onClick={onStopAnswering}
            className="inline-flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-[6px] text-ink-2 transition-colors duration-150 hover:bg-hover hover:text-ink motion-reduce:transition-none">
            <CloseGlyph />
          </button>
        </div>
      )}
      <textarea ref={ref} aria-label={asking ? "Question for the scout" : "Note to the scout"}
        placeholder={asking ? "Ask about this report…" : "Tell the scout what to check next…"}
        value={text} disabled={disabled} title={blocked ?? undefined} onChange={(event) => setText(event.target.value)}
        className="mt-2.5 block min-h-[88px] w-full resize-y rounded-[6px] border-0 bg-selected px-2.5 py-2 font-sans text-[14px] leading-[1.45] text-ink outline-none! placeholder:text-ink-3 focus:shadow-[0_0_0_2px_var(--wait)] disabled:cursor-not-allowed" />
      <div className="mt-2 flex justify-end [&>.btn]:shrink-0 [&>.btn]:whitespace-nowrap">
        <Btn variant="primary" busy={action.isPending} disabled={disabled || !text.trim()} title={blocked ?? undefined} onClick={() => { void send(); }}>
          Send
        </Btn>
      </div>
      {action.data?.ok === false && <p role="alert" className="mt-2 break-words font-sans text-[13px] leading-normal text-bad">{action.data.detail}</p>}
    </div>
  );
}

/** A scout's Thread at the end of its body: its messages and proposal cards, a line for each answer turn and pass
 *  between them, and the Ask and Steer composer, which Type an answer points at a card. */
export function ScoutThread({ base, detail, actions, ref }: { base: string; detail: ScoutDetail; actions: boolean; ref?: Ref<HTMLTextAreaElement> }) {
  const rows = threadRows(detail);
  const blocked = composeBlock(detail, actions);
  const sending = useSending(base);
  const [answeringId, setAnsweringId] = useState<number | null>(null);
  const headingId = useId();
  const answering = rows.find((row): row is CardRow => row.kind === "card" && row.id === answeringId && !row.answered) ?? null;
  const box = useRef<HTMLTextAreaElement | null>(null);
  const boxRef = useCallback((node: HTMLTextAreaElement | null) => {
    box.current = node;
    if (typeof ref === "function") ref(node);
    else if (ref) ref.current = node;
  }, [ref]);
  const typeAnswer = (card: CardRow) => {
    setAnsweringId(card.id);
    box.current?.focus();
  };
  return (
    <section id="thread" aria-labelledby={headingId} className="@container/agent mt-10 min-w-0">
      <h2 id={headingId} className={SCOUT_HEADING}>Thread</h2>
      {rows.length > 0 && (
        <ol aria-label="Thread" className="m-0 mt-2 flex list-none flex-col gap-3 p-0">
          {rows.map((row) => row.kind === "message"
            ? <ThreadMessage key={row.key} head={row.head} at={row.at} text={row.text} agent={row.agent} indent="ml-6" />
            : row.kind === "card"
            ? <ScoutCard key={row.key} base={base} scout={detail.scout.id} card={row} blocked={blocked} sending={sending} onType={() => typeAnswer(row)} />
            : (
              <li key={row.key} data-thread-line className={`flex items-center gap-2 font-mono text-[12px] leading-[1.4] [overflow-wrap:anywhere] before:h-px before:w-3 before:shrink-0 before:bg-rule before:content-[''] after:h-px after:w-3 after:shrink-0 after:bg-rule after:content-[''] ${LINE_TONES[row.tone]}`}>
                {row.text}
              </li>
            ))}
        </ol>
      )}
      <ScoutCompose base={base} detail={detail} actions={actions} sending={sending} answering={answering} onStopAnswering={() => setAnsweringId(null)} ref={boxRef} />
    </section>
  );
}
