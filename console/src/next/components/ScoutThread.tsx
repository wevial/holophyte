import { useState, type Ref } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { useAction } from "../actions";
import { composeBlock, nextPass, threadRows, type LineTone } from "../scoutThread";
import { Btn } from "./Btn";
import { SECTION_LABEL } from "./Criteria";
import { ThreadMessage } from "./ThreadMessage";

const LINE_TONES: Record<LineTone, string> = { quiet: "text-ink-3", bad: "text-bad", wait: "text-wait" };

type Mode = "ask" | "steer";
const MODES: { mode: Mode; label: string }[] = [{ mode: "ask", label: "Ask" }, { mode: "steer", label: "Steer" }];

function ScoutCompose({ base, detail, actions, ref }: { base: string; detail: ScoutDetail; actions: boolean; ref?: Ref<HTMLTextAreaElement> }) {
  const [mode, setMode] = useState<Mode>("ask");
  const [text, setText] = useState("");
  const action = useAction();
  const scout = detail.scout.id;
  const blocked = composeBlock(detail, actions);
  const disabled = blocked != null || action.isPending;
  const asking = mode === "ask";
  const send = async () => {
    if (disabled || !text.trim()) return;
    const result = await action.mutateAsync({
      label: asking ? "Ask" : "Steer", base, route: "/actions/scout-message",
      body: { scout, mode, text: text.trim() },
      said: asking ? `Asked scout ${scout}.` : `Steered scout ${scout}.`,
    });
    if (result.ok) setText("");
  };
  return (
    <div className="mt-4">
      <div role="group" aria-label="Message kind" className="inline-flex border border-ink-3">
        {MODES.map((each) => (
          <button key={each.mode} type="button" aria-pressed={mode === each.mode} disabled={disabled} title={blocked ?? undefined}
            className="min-h-9 px-3.5 font-mono text-[11px] font-medium uppercase leading-none tracking-[.1em] text-ink-2 aria-pressed:bg-ink aria-pressed:text-paper disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => { setMode(each.mode); action.reset(); }}>
            {each.label}
          </button>
        ))}
      </div>
      <textarea ref={ref} aria-label={asking ? "Question for the scout" : "Note to the scout"}
        placeholder={asking ? "Ask about this report…" : "Tell the scout what to check next…"}
        value={text} disabled={disabled} title={blocked ?? undefined} onChange={(event) => setText(event.target.value)}
        className="mt-2.5 block min-h-[88px] w-full resize-y border border-ink-3 bg-paper-2 p-2.5 font-read text-[14px] leading-[1.45] text-ink disabled:cursor-not-allowed" />
      <div className="mt-2.5 flex items-center justify-between gap-2.5 [&>.btn]:shrink-0 [&>.btn]:whitespace-nowrap">
        <span data-compose-line className="font-mono text-[12px] leading-[1.4] text-ink-2">
          {asking ? "Answers from the report." : `Starts pass ${nextPass(detail)}.`}
        </span>
        <Btn variant="primary" busy={action.isPending} disabled={disabled || !text.trim()} title={blocked ?? undefined} onClick={() => { void send(); }}>
          Send
        </Btn>
      </div>
      {action.data?.ok === false && <p role="alert" className="mt-2 break-words font-mono text-[12px] leading-normal text-bad">{action.data.detail}</p>}
    </div>
  );
}

/** A scout's Thread: its messages, a line for each answer turn and pass between them, and the Ask and Steer composer. */
export function ScoutThread({ base, detail, actions, ref }: { base: string; detail: ScoutDetail; actions: boolean; ref?: Ref<HTMLTextAreaElement> }) {
  const rows = threadRows(detail);
  return (
    <aside id="thread" aria-labelledby="thread-heading" className="@container/agent min-w-0 flex-[1_1_440px] border-t border-rule px-[22px] pb-6 pt-[22px] @[1000px]:min-w-[380px] @[1000px]:border-t-0 max-[720px]:px-4">
      <div id="thread-heading" className={SECTION_LABEL}>Thread</div>
      <div data-thread-lead className="mt-2 font-mono text-[12px] font-semibold leading-[1.5] text-ink">lead · {detail.scout.lead ?? "—"}</div>
      {rows.length > 0 && (
        <ol aria-label="Thread" className="m-0 mt-4 flex list-none flex-col gap-3 p-0">
          {rows.map((row) => row.kind === "message"
            ? <ThreadMessage key={row.key} head={row.head} at={row.at} text={row.text} agent={row.agent} />
            : (
              <li key={row.key} data-thread-line className={`flex items-center gap-2 font-mono text-[12px] leading-[1.4] [overflow-wrap:anywhere] before:h-px before:w-3 before:shrink-0 before:bg-rule before:content-[''] after:h-px after:w-3 after:shrink-0 after:bg-rule after:content-[''] ${LINE_TONES[row.tone]}`}>
                {row.text}
              </li>
            ))}
        </ol>
      )}
      <ScoutCompose key={detail.scout.id} base={base} detail={detail} actions={actions} ref={ref} />
    </aside>
  );
}
