import { TicketLink } from "./TicketLink";
import { useRunDetail } from "../hooks/useRunDetail";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { ReactNode } from "react";
import { isStale } from "../lib/derive";
import { mergeLockNote, workingMs } from "../lib/runs";
import { formatDuration, formatSpan } from "../lib/format";
import type { Run } from "../lib/types";
import { PhasePill } from "./PhasePill";
import { StrikePill } from "./StrikePill";
import { TimeBoxBar } from "./TimeBoxBar";

/** One live run: chevron, `#id`, ticket, title with its strike pill, phase,
 *  time box, heartbeat. Clicking toggles the detail slot beneath, where
 *  `detail` renders while the row is expanded. */
export function RunRow({
  run,
  thresholds,
  expanded,
  onToggle,
  detail,
  sinceMs = 0,
  base = "",
  polls = 0,
  deps = defaultPollDeps,
}: {
  run: Run;
  base?: string;
  polls?: number;
  deps?: { fetch: Fetch };
  thresholds: { heartbeat_stale_ms: number; strikes: number };
  expanded: boolean;
  onToggle: () => void;
  detail?: ReactNode;
  /** Local milliseconds since the daemon computed the run's numbers; the
   *  row adds it so they keep counting between polls. */
  sinceMs?: number;
}) {
  // Read PR and merge-lock wait events while the gate is active, including
  // when the row is collapsed.
  const { detail: prDetail } = useRunDetail(
    base, run.phase === "merge_gate" ? run.id : null, polls, deps,
  );
  const heartbeatAge = run.heartbeat_age_ms + sinceMs;
  const stale = isStale(heartbeatAge, thresholds.heartbeat_stale_ms);
  return (
    <li data-run={run.id} className="border-t border-line-faint">
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={onToggle}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onToggle();
          }
        }}
        className="grid w-full grid-cols-[20px_64px_110px_1fr_120px_220px_90px] items-center gap-3 px-4 py-[11px] text-left hover:bg-hover"
      >
        <span aria-hidden="true" className="text-[12px] text-faint">
          {expanded ? "▾" : "▸"}
        </span>
        <span className="font-mono text-[12px] text-muted">#{run.id}</span>
        <span className="truncate font-mono text-[13px] font-semibold text-ink"><TicketLink ticket={run.ticket} ticket_url={run.ticket_url} /></span>
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[14px] text-body">{run.title ?? ""}</span>
          <StrikePill strikes={run.strikes ?? 0} max={thresholds.strikes} />
        </span>
        <span>
          <PhasePill note={mergeLockNote(prDetail?.events)} phase={run.phase} pr_url={run.pr_url ?? prDetail?.run.pr_url} />
        </span>
        <span><TimeBoxBar elapsedMs={workingMs(run, sinceMs)} boxMs={run.time_box_ms} />
          <span className="font-mono text-[12px] text-muted">wall {formatDuration(run.elapsed_ms + sinceMs)}</span></span>
        <span
          data-heartbeat={stale ? "stale" : "live"}
          className={`font-mono text-[12px] ${stale ? "font-semibold text-bad" : "text-ok-text"}`}
        >
          hb {formatSpan(heartbeatAge)}
        </span>
      </div>
      {expanded && (detail ?? <div data-detail className="border-t border-line-faint px-4 py-3" />)}
    </li>
  );
}
