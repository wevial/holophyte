import { formatDuration } from "../lib/format";
import { prLabel } from "../lib/shipped";
import type { Queued } from "../lib/types";
import { PhasePill } from "./PhasePill";
import { TicketLink } from "./TicketLink";

/** A ticket released to the ready queue, waiting for a worker: ticket,
 *  title over the first line of why it waits (all of it on hover), the
 *  Queued pill, its pull request and the wait so far. */
export function QueuedRow({ entry, sinceMs = 0 }: { entry: Queued; sinceMs?: number }) {
  const reason = entry.reason ?? "";
  return (
    <li data-queued={entry.ticket} className="border-t border-line-faint">
      <div className="grid w-full grid-cols-[20px_64px_110px_1fr_120px_220px_90px] items-center gap-3 px-4 py-[11px] text-left">
        <span />
        <span />
        <span className="truncate font-mono text-[13px] font-semibold text-ink"><TicketLink ticket={entry.ticket} ticket_url={entry.ticket_url} /></span>
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[14px] text-body">{entry.title ?? ""}</span>
          {reason && <span title={reason} className="min-w-0 truncate text-[12px] text-muted">{reason.split("\n")[0]}</span>}
        </span>
        <span><PhasePill phase="Queued" /></span>
        <span>
          {entry.pr_url && (
            <a className="font-mono text-[12px] text-link" href={entry.pr_url} target="_blank" rel="noopener noreferrer">{prLabel(entry.pr_url)}</a>
          )}
        </span>
        <span className="whitespace-nowrap font-mono text-[12px] text-muted">waiting {formatDuration(entry.waiting_ms + sinceMs)}</span>
      </div>
    </li>
  );
}
