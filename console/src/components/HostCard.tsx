import { formatDuration } from "../lib/format";
import { isSupervisorStale } from "../lib/derive";
import type { Status } from "../lib/types";

/** The serving daemon's card in the rail footer. */
export function HostCard({ status, port }: { status: Status; port: string }) {
  const { supervisor } = status;
  const stale = isSupervisorStale(supervisor, status.thresholds.heartbeat_stale_ms);
  const heartbeat =
    supervisor.heartbeat_age_ms == null ? "no hb" : `hb ${formatDuration(supervisor.heartbeat_age_ms)}`;
  const runs = `${status.runs.length} ${status.runs.length === 1 ? "run" : "runs"}`;
  const uptime = status.daemon ? `daemon up ${formatDuration(status.now - status.daemon.started_ms)} · ` : "";
  return (
    <div
      data-stale={stale || undefined}
      className={`rounded-card bg-rail-card p-2.5 ${stale ? "border border-bad/50" : ""}`}
    >
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className={`size-2 shrink-0 rounded-chip ${stale ? "animate-[pulse-dot_1.2s_ease-in-out_infinite] bg-bad" : "bg-ok"}`}
        />
        <span className="truncate text-[13px] font-semibold text-rail-text">{status.host}</span>
        <span className={`ml-auto font-mono text-[11px] ${stale ? "text-rail-bad-text" : "text-rail-sub"}`}>
          {port} · {heartbeat}
        </span>
      </div>
      <div className="mt-1 font-mono text-[10px] text-rail-faint">
        {uptime}
        {runs}
      </div>
    </div>
  );
}
