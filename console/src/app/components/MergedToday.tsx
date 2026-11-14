import { Sha } from "./MergeFacts";
import { formatClock } from "../../lib/format";
import { prLabel } from "../../lib/shipped";
import type { ShippedRow } from "../../lib/types";
import { StateDot } from "./StateDot";
import { TitleText } from "./TitleText";

/** How many of today's merges the Overview lists before its "N more" link. */
export const SHOWN_MERGES = 3;

/** Today's merges as hairline rows, newest first, each with its project's dot. */
export function MergedToday({ rows, colours }: { rows: ShippedRow[]; colours: Map<string, string> }) {
  return (
    <ul aria-label="Today's merges">
      {rows.map((row) => (
        <li
          key={`${row.daemon ?? ""}:${row.id}`}
          data-merge={row.ticket}
          className="grid min-h-10 grid-cols-[16px_84px_minmax(0,1fr)_auto] items-center gap-x-3 border-b border-rule-2 px-2.5 font-mono text-[12px] text-ink-3 max-[720px]:grid-cols-[16px_minmax(0,1fr)_auto]"
        >
          <StateDot state="merged" hideWord />
          <span className="truncate text-ink-2 max-[720px]:hidden">{row.ticket}</span>
          <span className="truncate font-read text-[14.5px] font-medium text-ink">
            <TitleText>{row.title ?? row.ticket}</TitleText>
          </span>
          <span className="flex items-center gap-3 whitespace-nowrap">
            <span role="img" aria-label={row.project} title={row.project} className="block size-1.5 rounded-full bg-ink-3" style={{ background: colours.get(row.project) }} />
            {row.pr_url ? (
              <a data-pr href={row.pr_url} title={row.merge_sha ?? undefined} target="_blank" rel="noopener noreferrer" className="hover:text-ink">
                {prLabel(row.pr_url).replace(/^PR (?=#)/, "")}
              </a>
            ) : (
              <Sha row={row} />
            )}
            <time>{formatClock(row.ended_ms)}</time>
          </span>
        </li>
      ))}
    </ul>
  );
}
