import { useState, type ReactNode } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import { useAction } from "../actions";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import { Glyph } from "./AgentPanel";
import { Btn } from "./Btn";

const ASK_OFF = "Ask opens once the run parks on its pull request.";
const TIP = "z-50 rounded-[6px] bg-ink px-2 py-1 font-sans text-[12px] font-medium leading-[1.2] whitespace-nowrap text-canvas";
const HINT = <path d="M6 12.5h4M6.5 14h3M8 2a4 4 0 0 0-2.4 7.2c.5.4.9 1 .9 1.6v.2h3v-.2c0-.6.4-1.2.9-1.6A4 4 0 0 0 8 2z" />;
const STOP = <path d="M8.5 2 4 9h4l-.5 5L12 7H8z" />;

function Toggle({ name, pressed, disabled, onPress, children }: {
  name: string;
  pressed: boolean;
  disabled: boolean;
  onPress: () => void;
  children: ReactNode;
}) {
  return (
    <Tooltip delay={150}>
      <TooltipTrigger
        render={(trigger) => (
          <button {...trigger} type="button" aria-label={name} aria-pressed={pressed} disabled={disabled} onClick={onPress}
            className="inline-grid size-7 place-items-center rounded-[6px] text-ink-3 hover:bg-hover hover:text-ink aria-pressed:bg-selected aria-pressed:text-ink disabled:cursor-not-allowed disabled:opacity-40">
            <Glyph size={16}>{children}</Glyph>
          </button>
        )}
      />
      <TooltipContent side="top" sideOffset={6} className={TIP}>{name}</TooltipContent>
    </Tooltip>
  );
}

export function AgentCompose({ base, run, ticket, actions, parked, live }: {
  base: string;
  run: number;
  ticket: string;
  actions: boolean;
  parked: boolean;
  live: boolean;
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
      said: asking ? "Asked. The answer appears here." : `Steered ${ticket}.`,
    });
    if (result.ok) {
      setText("");
      setHint(false);
      setNow(false);
    }
  };
  return (
    <div data-compose className="flex flex-col gap-2.5">
      <div role="group" aria-label="Message kind" className="inline-flex self-start rounded-[7px] bg-canvas p-0.5">
        {(["Steer", "Ask"] as const).map((mode) => (
          <button key={mode} type="button" aria-pressed={kind === mode}
            disabled={disabled || (mode === "Ask" && !parked)}
            title={!actions ? ACTIONS_OFF : mode === "Ask" && !parked ? ASK_OFF : undefined}
            className="h-[26px] rounded-[5px] px-3 font-sans text-[12px] font-medium leading-none text-ink-3 hover:text-ink aria-pressed:bg-selected aria-pressed:text-ink disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-ink-3"
            onClick={() => { setKind(mode); action.reset(); }}>
            {mode}
          </button>
        ))}
      </div>
      <textarea aria-label={asking ? "Question for the agent" : "Note to the agent"}
        placeholder={asking ? "Ask about this run…" : "Tell the agent what to change…"}
        value={text} disabled={disabled} onChange={(event) => setText(event.target.value)}
        className="block min-h-[84px] w-full resize-y rounded-[8px] border border-rule-2 bg-canvas px-3 py-2.5 font-sans text-[14px] leading-[1.45] text-ink placeholder:text-ink-3 focus:border-wait focus:outline-none" />
      <div className="flex items-center gap-1 [&>.btn]:ml-auto [&>.btn]:shrink-0 [&>.btn]:whitespace-nowrap">
        {!asking && <Toggle name="Just a hint" pressed={hint} disabled={disabled} onPress={() => setHint(!hint)}>{HINT}</Toggle>}
        {!asking && live && <Toggle name="Stop it now" pressed={now} disabled={disabled} onPress={() => setNow(!now)}>{STOP}</Toggle>}
        <Btn variant={stop ? "danger-fill" : "primary"} busy={action.isPending} disabled={disabled || unavailable || !text.trim()}
          title={!actions ? ACTIONS_OFF : unavailable ? ASK_OFF : undefined} onClick={() => { void send(); }}>
          {stop ? "Stop and send" : "Send"}
        </Btn>
      </div>
      <span className={`font-mono text-[12px] leading-[1.4] ${stop ? "text-bad" : "text-ink-3"}`}>{sentence}</span>
      {action.data?.ok === false && <p role="alert" className="m-0 break-words font-mono text-[12px] leading-normal text-bad">{action.data.detail}</p>}
    </div>
  );
}
