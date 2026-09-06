import type { ReactNode } from "react";
import { isStale } from "../lib/derive";
import { formatDuration } from "../lib/format";
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
}: {
  run: Run;
  thresholds: { heartbeat_stale_ms: number; strikes: number };
  expanded: boolean;
  onToggle: () => void;
  detail?: ReactNode;
}) {
  const stale = isStale(run.heartbeat_age_ms, thresholds.heartbeat_stale_ms);
  return (
    <li data-run={run.id} className="border-t border-line-faint">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={onToggle}
        className="grid w-full grid-cols-[20px_64px_110px_1fr_120px_220px_90px] items-center gap-3 px-4 py-[11px] text-left hover:bg-hover"
      >
        <span aria-hidden="true" className="text-[12px] text-faint">
          {expanded ? "▾" : "▸"}
        </span>
        <span className="font-mono text-[12px] text-muted">#{run.id}</span>
        <span className="truncate font-mono text-[13px] font-semibold text-ink">{run.ticket}</span>
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[14px] text-body">{run.title ?? ""}</span>
          <StrikePill strikes={run.strikes ?? 0} max={thresholds.strikes} />
        </span>
        <span>
          <PhasePill phase={run.phase} />
        </span>
        <TimeBoxBar elapsedMs={run.elapsed_ms} boxMs={run.time_box_ms} />
        <span
          data-heartbeat={stale ? "stale" : "live"}
          className={`font-mono text-[12px] ${stale ? "font-semibold text-bad" : "text-ok-text"}`}
        >
          hb {formatDuration(run.heartbeat_age_ms)}
        </span>
      </button>
      {expanded && (detail ?? <div data-detail className="border-t border-line-faint px-4 py-3" />)}
    </li>
  );
}
