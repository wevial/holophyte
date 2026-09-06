import type { ReactNode } from "react";
import { isSupervisorStale } from "../lib/derive";
import { formatDuration } from "../lib/format";
import type { ProjectGroup } from "../lib/runs";
import type { Run } from "../lib/types";
import { RunRow } from "./RunRow";

/** One project's card: the daemon's host and supervisor line over a row
 *  per live run. */
export function ProjectBlock({
  group,
  expandedRun,
  onToggleRun,
  renderDetail,
}: {
  group: ProjectGroup;
  expandedRun: number | null;
  onToggleRun: (id: number) => void;
  renderDetail?: (run: Run) => ReactNode;
}) {
  const { status } = group;
  const { supervisor, thresholds } = status;
  const stale = isSupervisorStale(supervisor, thresholds.heartbeat_stale_ms);
  const dot = stale ? "bg-bad" : supervisor.state === "live" ? "bg-ok" : "bg-faint";
  const heartbeat = supervisor.heartbeat_age_ms == null ? "no hb" : `hb ${formatDuration(supervisor.heartbeat_age_ms)}`;
  return (
    <section
      aria-label={group.name}
      data-supervisor={stale ? "stale" : supervisor.state}
      className="overflow-hidden rounded-[10px] border border-line bg-card shadow-card"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1 bg-card-header px-4 py-[10px]">
        <span aria-hidden="true" className={`size-2 shrink-0 rounded-chip ${dot}`} />
        <span className="text-[15px] font-semibold text-ink">{group.name}</span>
        <span className="truncate font-mono text-[12px] text-faint">{group.path}</span>
        <span
          data-supervisor-line
          className={`ml-auto font-mono text-[12px] ${stale ? "font-semibold text-bad" : "text-ok-text"}`}
        >
          on <strong className="font-bold">{status.host}</strong> · supervisor {stale ? "stale" : supervisor.state} ·{" "}
          {heartbeat}
        </span>
      </header>
      <ul>
        {group.runs.map((run) => (
          <RunRow
            key={run.id}
            run={run}
            thresholds={thresholds}
            expanded={expandedRun === run.id}
            onToggle={() => onToggleRun(run.id)}
            detail={expandedRun === run.id ? renderDetail?.(run) : undefined}
          />
        ))}
      </ul>
    </section>
  );
}
