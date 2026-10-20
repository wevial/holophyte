import type { ReactNode } from "react";
import { Markdown } from "../../components/Markdown";
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
            <li key={message.key} className={`border px-3 py-2.5 font-read text-[14px] leading-[1.45] text-ink [overflow-wrap:anywhere] ${message.agent ? "border-rule bg-canvas" : "border-ink-3 bg-paper-2 @[396px]/agent:ml-6"}`}>
              <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 font-mono text-[11px] font-medium leading-[1.3] tracking-[.1em] text-ink-2">
                <span>{message.head} · {formatClock(message.at)}</span>
                {message.url && <a href={message.url} target="_blank" rel="noreferrer noopener" className="text-ink-3 underline underline-offset-2 hover:text-ink">Open on GitHub <span aria-hidden="true">↗</span></a>}
              </div>
              <div className="[&_:is(p,ul,ol,pre,blockquote)+*]:mt-2 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li+li]:mt-0.5 [&_code]:font-mono [&_code]:text-[12.5px] [&_pre]:whitespace-pre-wrap [&_pre]:border [&_pre]:border-rule [&_pre]:px-2.5 [&_pre]:py-2 [&_blockquote]:border-l-2 [&_blockquote]:border-rule [&_blockquote]:pl-3 [&_a]:underline">
                <Markdown>{message.text}</Markdown>
              </div>
              {message.effect && <div className="mt-1.5 font-mono text-[12px] text-ink-3">{message.effect}</div>}
            </li>
          ))}
        </ol>
      )}
      {children}
    </aside>
  );
}
