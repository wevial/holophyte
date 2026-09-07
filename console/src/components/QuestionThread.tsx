import { formatClock } from "../lib/format";
import type { ThreadRow } from "../lib/threads";
import { ActionButton } from "./ActionButton";

/** The placeholder of the answer field until writes land (KO-261). */
export const ANSWER_PLACEHOLDER = "Type an answer… the run resumes with it as ticket context.";

/** A question's thread under its row: the ledger rows of the blocked run
 *  oldest first (who · text · time), then the inert answer footer. */
export function QuestionThread({ rows }: { rows: ThreadRow[] }) {
  return (
    <div
      data-thread
      className="my-2 mb-3 ml-[110px] overflow-hidden rounded-[10px] border border-line bg-card"
    >
      <ol className="list-none">
        {rows.map((row, index) => (
          <li
            key={`${row.at ?? "asked"}:${index}`}
            data-thread-row
            className={`grid grid-cols-[64px_1fr_auto] gap-3 px-[14px] py-[10px] ${index > 0 ? "border-t border-line-faint" : ""}`}
          >
            <span data-who className="font-mono text-[12px] font-semibold text-muted">
              {row.who}
            </span>
            <span className="min-w-0 break-words text-[13px] leading-[1.45] text-body">{row.text}</span>
            <span className="font-mono text-[11px] text-faint">{row.at == null ? "" : formatClock(row.at)}</span>
          </li>
        ))}
      </ol>
      <div className="bg-card-header px-[14px] py-3">
        <textarea
          disabled
          aria-label="Answer"
          placeholder={ANSWER_PLACEHOLDER}
          className="block min-h-[64px] w-full rounded-[8px] border border-chip-border bg-paper px-3 py-2 text-[13px] placeholder:text-muted"
        />
        <div className="mt-2 flex justify-end">
          <ActionButton>Answer &amp; resume</ActionButton>
        </div>
      </div>
    </div>
  );
}
