import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import type { ConsoleAsk, SteerNote } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { noteAuthor } from "../progress";
import { Markdown } from "./Markdown";
import { STATE_ICONS } from "./StateDot";

export const TIP = "z-50 rounded-[6px] bg-ink px-2 py-1 font-sans text-[12px] font-medium leading-[1.2] whitespace-nowrap text-canvas";

const STOPPED = (
  <>
    <circle cx="8" cy="8" r="6.5" />
    <rect x="5.9" y="5.9" width="4.2" height="4.2" rx=".6" fill="currentColor" stroke="none" />
  </>
);

export const GLYPHS = {
  steer: <path d="M2.5 8h9M8.5 4.5 12 8l-3.5 3.5" />,
  hint: <path d="M6 12.5h4M6.5 14h3M8 2a4 4 0 0 0-2.4 7.2c.5.4.9 1 .9 1.6v.2h3v-.2c0-.6.4-1.2.9-1.6A4 4 0 0 0 8 2z" />,
  ask: (
    <>
      <path d="M3 4.5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2V9a2 2 0 0 1-2 2H7l-3 2.5V11a2 2 0 0 1-1-1.7z" />
      <path d="M6.9 5.6a1.2 1.2 0 1 1 1.6 1.1c-.3.1-.5.4-.5.7M8 9h.1" />
    </>
  ),
  reply: <path d="M3 3v5a2 2 0 0 0 2 2h8M10 7l3 3-3 3" />,
};

export function Glyph({ children, size = 14, className = "" }: { children: ReactNode; size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.6}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`shrink-0 ${className}`}>
      {children}
    </svg>
  );
}

/** A glyph named by its tooltip, which opens alike on hover and keyboard focus. */
function GlyphTip({ tip, className = "", children }: { tip: string; className?: string; children: ReactNode }) {
  return (
    <Tooltip delay={150}>
      <TooltipTrigger
        render={({ type: _button, ...trigger }) => (
          <span {...trigger} role="img" aria-label={tip} tabIndex={0} className={`inline-flex items-center rounded-[4px] outline-offset-2 ${className}`}>
            {children}
          </span>
        )}
      />
      <TooltipContent side="top" sideOffset={6} className={TIP}>{tip}</TooltipContent>
    </Tooltip>
  );
}

const ANSWER = "[&_:is(p,ul,ol,pre,blockquote)+*]:mt-2 [&_p]:m-0 [&_p]:whitespace-pre-line [&_ul]:m-0 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:m-0 [&_ol]:list-decimal [&_ol]:pl-5 [&_li+li]:mt-0.5 [&_code]:font-mono [&_code]:text-[12px] [&_pre]:whitespace-pre-wrap [&_pre]:rounded-[6px] [&_pre]:bg-canvas [&_pre]:px-2.5 [&_pre]:py-2 [&_blockquote]:border-l-2 [&_blockquote]:border-rule [&_blockquote]:pl-3 [&_a]:underline";

interface Read {
  tip: string;
  at: number | null;
}

interface Message {
  key: string;
  at: number;
  author: string;
  kind: keyof typeof GLYPHS;
  text: string;
  read?: Read;
  reply?: { at: number; text: string };
}

function readOf(note: SteerNote): Read {
  if (note.withdrawn) return { tip: "withdrawn", at: null };
  if (note.consumed_ms == null) return { tip: "waiting until the agent reads it", at: null };
  return { tip: `the agent read it at ${formatClock(note.consumed_ms)}`, at: note.consumed_ms };
}

function ReadMark({ read }: { read: Read }) {
  const icon = read.at != null ? STATE_ICONS.ready_to_merge : read.tip === "withdrawn" ? STATE_ICONS.abandoned : STATE_ICONS.queued;
  return (
    <>
      <GlyphTip tip={read.tip} className={read.at != null ? "text-live" : "text-ink-3"}><Glyph>{icon}</Glyph></GlyphTip>
      {read.at != null && <span>{formatClock(read.at)}</span>}
    </>
  );
}

function Note({ message }: { message: Message }) {
  return (
    <li data-note className="flex flex-col gap-1 font-read text-[13px] leading-[1.45] text-ink-2 [overflow-wrap:anywhere]">
      <div className={ANSWER}><Markdown>{message.text}</Markdown></div>
      {message.reply && (
        <div data-reply className="flex gap-1.5 text-ink">
          <Glyph className="mt-[3px] text-ink-3">{GLYPHS.reply}</Glyph>
          <div className={`min-w-0 ${ANSWER}`}><Markdown>{message.reply.text}</Markdown></div>
        </div>
      )}
      <div data-meta className="flex flex-wrap items-center gap-1.5 font-mono text-[12px] leading-[1.4] text-ink-3">
        <span>{message.author}</span>
        <GlyphTip tip={message.kind}><Glyph size={13}>{GLYPHS[message.kind]}</Glyph></GlyphTip>
        <span>{formatClock(message.at)}</span>
        {message.read && <ReadMark read={message.read} />}
        {message.reply && (
          <>
            <GlyphTip tip="answer" className="ml-1"><Glyph size={13}>{GLYPHS.reply}</Glyph></GlyphTip>
            <span>{formatClock(message.reply.at)}</span>
          </>
        )}
      </div>
    </li>
  );
}

export function AgentPanel({ label, state, live, notes, asks, children }: {
  label: string;
  state: string;
  live: boolean;
  notes: SteerNote[];
  asks: ConsoleAsk[];
  children?: ReactNode;
}) {
  const messages: Message[] = notes.map((note) => ({
    key: `note-${note.id}`,
    at: note.at,
    author: noteAuthor(note.author, note.author),
    kind: note.kind === "hint" ? "hint" : "steer",
    text: note.note,
    read: readOf(note),
  }));
  for (const ask of asks) {
    messages.push({
      key: `ask-${ask.id}`, at: ask.asked_ms, author: noteAuthor(ask.author, ask.author), kind: "ask", text: ask.question,
      reply: ask.answer != null && ask.answered_ms != null ? { at: ask.answered_ms, text: ask.answer } : undefined,
    });
  }
  messages.sort((a, b) => a.at - b.at);
  return (
    <aside id="agent" aria-label="Agent" className="@container/agent flex min-w-0 flex-col gap-3.5 rounded-[10px] bg-raised p-4 min-[1180px]:sticky min-[1180px]:top-5">
      <div data-agent-head className="flex items-center gap-2 font-sans text-[13px] font-semibold text-ink">
        <GlyphTip tip={state} className={live ? "text-live" : "text-ink-3"}>
          <Glyph>{live ? STATE_ICONS.running : STOPPED}</Glyph>
        </GlyphTip>
        Implementer
        <span className="ml-auto font-mono text-[12px] font-normal text-ink-3">{label}</span>
      </div>
      {messages.length === 0 ? (
        <p className="m-0 font-mono text-[12px] text-ink-3">Nothing sent to this run yet.</p>
      ) : (
        <ol className="m-0 flex list-none flex-col gap-3.5 p-0">
          {messages.map((message) => <Note key={message.key} message={message} />)}
        </ol>
      )}
      {children}
    </aside>
  );
}
