import type { ConsoleAsk, SteerNote } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { SECTION_LABEL } from "./Criteria";

interface Message {
  key: string;
  at: number;
  head: string;
  text: string;
  agent?: boolean;
  url?: string | null;
  effect?: string;
}

function thread(notes: SteerNote[], asks: ConsoleAsk[]): Message[] {
  const messages: Message[] = notes.map((note) => ({
    key: `note-${note.id}`,
    at: note.at,
    head: `${note.author === "maintainer" ? "You" : note.author} · ${note.kind === "hint" ? "hint" : "steer"}`,
    text: note.note,
    effect: note.withdrawn ? "→ withdrawn" : note.consumed_ms == null
      ? "→ waiting until the agent reads it" : `→ the agent read it at ${formatClock(note.consumed_ms)}`,
  }));
  for (const ask of asks) {
    messages.push({ key: `ask-${ask.id}`, at: ask.asked_ms, head: "You · ask", text: ask.question });
    if (ask.answer != null && ask.answered_ms != null) {
      messages.push({ key: `answer-${ask.id}`, at: ask.answered_ms, head: "Agent", text: ask.answer, agent: true, url: ask.url });
    }
  }
  return messages.sort((a, b) => a.at - b.at);
}

export function AgentPanel({ label, state, notes, asks }: {
  label: string;
  state: string;
  notes: SteerNote[];
  asks: ConsoleAsk[];
}) {
  const messages = thread(notes, asks);
  return (
    <aside id="agent" aria-labelledby="agent-heading" className="min-w-0 flex-[1_1_320px] border-t border-rule px-[22px] pb-6 pt-[22px] @[880px]:border-t-0 max-[720px]:px-4">
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
            <li key={message.key} className={`border px-3 py-2.5 font-plex text-[14px] leading-[1.45] text-ink [overflow-wrap:anywhere] ${message.agent ? "border-rule bg-canvas" : "ml-6 border-ink-3 bg-paper-2"}`}>
              <div className="mb-1.5 font-mono text-[11px] font-medium leading-[1.3] tracking-[.1em] text-ink-2">
                {message.head} · {formatClock(message.at)}
              </div>
              <div className="whitespace-pre-wrap">
                {message.url ? <a href={message.url} className="text-act underline underline-offset-2">{message.text}</a> : message.text}
              </div>
              {message.effect && <div className="mt-1.5 font-mono text-[12px] text-ink-3">{message.effect}</div>}
            </li>
          ))}
        </ol>
      )}
    </aside>
  );
}
