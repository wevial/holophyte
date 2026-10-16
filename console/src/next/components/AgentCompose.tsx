import { useState, type Ref } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import { prLabel } from "../../lib/shipped";
import { useAction } from "../actions";
import { Btn } from "./Btn";

const ASK_OFF = "Questions are answered on the pull request, once the run parks on it.";

export function AgentCompose({ base, run, ticket, prUrl, actions, parked, live, ref }: {
  base: string;
  run: number;
  ticket: string;
  prUrl: string | null;
  actions: boolean;
  parked: boolean;
  live: boolean;
  ref?: Ref<HTMLTextAreaElement>;
}) {
  const [kind, setKind] = useState<"Steer" | "Ask">("Steer");
  const [text, setText] = useState("");
  const [hint, setHint] = useState(false);
  const [now, setNow] = useState(false);
  const action = useAction();
  const asking = kind === "Ask";
  const stop = !asking && live && now;
  const disabled = !actions || action.isPending;
  const unavailable = asking && !parked;
  const sentence = asking ? "The run keeps going." : stop ? "Stops now and restarts with your note. Nothing is lost."
    : hint ? "The agent may take it or leave it." : "Read on its next turn.";
  const send = async () => {
    if (disabled || unavailable || !text.trim()) return;
    const result = await action.mutateAsync({
      label: kind, base, route: asking ? "/actions/ask" : "/actions/steer",
      body: asking ? { run, question: text.trim() } : { ticket, note: text.trim(), hint, now: stop },
      said: asking ? `Asked on ${prUrl == null ? "the pull request" : prLabel(prUrl)}.` : `Steered ${ticket}.`,
    });
    if (result.ok) {
      setText("");
      setHint(false);
      setNow(false);
    }
  };
  return (
    <div className="mt-4">
      <div role="group" aria-label="Message kind" className="inline-flex border border-ink-3">
        {(["Steer", "Ask"] as const).map((mode) => (
          <button key={mode} type="button" aria-pressed={kind === mode}
            disabled={disabled || (mode === "Ask" && !parked)}
            title={!actions ? ACTIONS_OFF : mode === "Ask" && !parked ? ASK_OFF : undefined}
            className="min-h-9 px-3.5 font-mono text-[11px] font-medium uppercase leading-none tracking-[.1em] text-ink-2 aria-pressed:bg-ink aria-pressed:text-paper disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => { setKind(mode); action.reset(); }}>
            {mode}
          </button>
        ))}
      </div>
      <textarea ref={ref} aria-label={asking ? "Question for the agent" : "Note to the agent"}
        placeholder={asking ? "Ask about this run…" : "Tell the agent what to change…"}
        value={text} disabled={disabled} onChange={(event) => setText(event.target.value)}
        className="mt-2.5 block min-h-[88px] w-full resize-y border border-ink-3 bg-paper-2 p-2.5 font-plex text-[14px] leading-[1.45] text-ink" />
      {!asking && <div className="mt-1 flex flex-wrap gap-x-4 font-mono text-[12px] leading-[1.4] text-ink">
        <label className="inline-flex min-h-10 items-center gap-2">
          <input type="checkbox" checked={hint} disabled={disabled} onChange={(event) => setHint(event.target.checked)} className="m-0 accent-act" />Just a hint
        </label>
        {live && <label className="inline-flex min-h-10 items-center gap-2">
          <input type="checkbox" checked={now} disabled={disabled} onChange={(event) => setNow(event.target.checked)} className="m-0 accent-bad" />Stop it now
        </label>}
      </div>}
      <div className="mt-2.5 flex items-center justify-between gap-2.5 [&>.btn]:shrink-0 [&>.btn]:whitespace-nowrap">
        <span className={`font-mono text-[12px] leading-[1.4] ${stop ? "text-bad" : "text-ink-2"}`}>{sentence}</span>
        <Btn variant={stop ? "danger-fill" : "primary"} busy={action.isPending} disabled={disabled || unavailable || !text.trim()}
          title={!actions ? ACTIONS_OFF : unavailable ? ASK_OFF : undefined} onClick={() => { void send(); }}>
          {stop ? "Stop and send" : "Send"}
        </Btn>
      </div>
      {action.data?.ok === false && <p role="alert" className="mt-2 break-words font-mono text-[12px] leading-normal text-bad">{action.data.detail}</p>}
    </div>
  );
}
