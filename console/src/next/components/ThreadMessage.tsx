import { Markdown } from "../../components/Markdown";
import { formatClock } from "../../lib/format";

/** One message of a thread: a person's indented on their background, an agent's flush. */
export function ThreadMessage({ head, at, text, agent = false, effect, indent = "@[396px]/agent:ml-6" }: {
  head: string;
  at: number;
  text: string;
  agent?: boolean;
  effect?: string;
  indent?: string;
}) {
  return (
    <li className={`border px-3 py-2.5 font-read text-[14px] leading-[1.45] text-ink [overflow-wrap:anywhere] ${agent ? "border-rule bg-canvas" : `border-ink-3 bg-paper-2 ${indent}`}`}>
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 font-mono text-[11px] font-medium leading-[1.3] tracking-[.1em] text-ink-2">
        <span>{head} · {formatClock(at)}</span>
      </div>
      <div className="[&_:is(p,ul,ol,pre,blockquote)+*]:mt-2 [&_p]:whitespace-pre-line [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li+li]:mt-0.5 [&_code]:font-mono [&_code]:text-[12.5px] [&_pre]:whitespace-pre-wrap [&_pre]:border [&_pre]:border-rule [&_pre]:px-2.5 [&_pre]:py-2 [&_blockquote]:border-l-2 [&_blockquote]:border-rule [&_blockquote]:pl-3 [&_a]:underline">
        <Markdown>{text}</Markdown>
      </div>
      {effect && <div className="mt-1.5 font-mono text-[12px] text-ink-3">{effect}</div>}
    </li>
  );
}
