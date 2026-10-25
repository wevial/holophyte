import type { ReactNode } from "react";
import type { ConsoleAsk, SteerNote } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { noteAuthor } from "../progress";
import { SECTION_LABEL } from "./Criteria";
import { ThreadMessage } from "./ThreadMessage";

interface Message {
  key: string;
  at: number;
  head: string;
  text: string;
  agent?: boolean;
  effect?: string;
}

export function AgentPanel({ label, state, notes, asks, children }: {
  label: string;
  state: string;
  notes: SteerNote[];
  asks: ConsoleAsk[];
  children?: ReactNode;
}) {
  const messages: Message[] = notes.map((note) => ({
    key: `note-${note.id}`,
    at: note.at,
    head: `${noteAuthor(note.author, note.author)} · ${note.kind === "hint" ? "hint" : "steer"}`,
    text: note.note,
    effect: note.withdrawn ? "→ withdrawn" : note.consumed_ms == null
      ? "→ waiting until the agent reads it" : `→ the agent read it at ${formatClock(note.consumed_ms)}`,
  }));
  for (const ask of asks) {
    messages.push({ key: `ask-${ask.id}`, at: ask.asked_ms, head: "You · ask", text: ask.question });
    if (ask.answer != null && ask.answered_ms != null) {
      messages.push({ key: `answer-${ask.id}`, at: ask.answered_ms, head: "Agent", text: ask.answer, agent: true });
    }
  }
  messages.sort((a, b) => a.at - b.at);
  return (
    <aside id="agent" aria-labelledby="agent-heading" className="@container/agent min-w-0 flex-[1_1_440px] border-t border-rule px-[22px] pb-6 pt-[22px] @[1000px]:min-w-[380px] @[1000px]:border-t-0 max-[720px]:px-4">
      <div id="agent-heading" className={SECTION_LABEL}>Agent</div>
      <div className="mt-2 font-mono text-[12px] leading-[1.5] text-ink-2">
        <div className="font-semibold text-ink">implementer · {label}</div>
        <div>{state}</div>
      </div>
      {messages.length === 0 ? (
        <p className="mt-4 font-mono text-[12px] text-ink-3">Nothing sent to this run yet.</p>
      ) : (
        <ol className="m-0 mt-4 flex list-none flex-col gap-3 p-0">
          {messages.map((message) => (
            <ThreadMessage key={message.key} head={message.head} at={message.at} text={message.text} agent={message.agent} effect={message.effect} />
          ))}
        </ol>
      )}
      {children}
    </aside>
  );
}
