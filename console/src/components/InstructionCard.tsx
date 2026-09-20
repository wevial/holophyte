import type { Instruction } from "../lib/findings";
import { renderCommentBody } from "../lib/markdown";

export function InstructionCard({ instruction }: { instruction: Instruction }) {
  const location = instruction.line != null ? `${instruction.path}:${instruction.line}` : instruction.path;
  const state = instruction.outcome ?? "awaiting fix";
  const tone = state === "changed" ? "bg-ok-bg text-ok-text"
    : state === "kept" ? "border border-chip-border text-muted" : "bg-warn-bg text-warn-text";
  return (
    <li data-instruction className="rounded-[10px] border border-line bg-card px-4 py-[14px] shadow-card">
      <div className="flex flex-wrap items-center gap-2">
        <span data-outcome={state} className={`inline-block shrink-0 rounded-pill px-2 py-[3px] font-mono text-[11px] font-semibold ${tone}`}>
          {state}
        </span>
        {instruction.url ? (
          <a href={instruction.url} className="truncate font-mono text-[12px] text-link">{location}</a>
        ) : <span className="truncate font-mono text-[12px] text-muted">{location}</span>}
        <span className="text-[12px] text-muted">@{instruction.author}</span>
      </div>
      <div className="ticket-body mt-1 text-[14px] leading-[1.45] text-body">
        {renderCommentBody(instruction.request)}
      </div>
    </li>
  );
}
