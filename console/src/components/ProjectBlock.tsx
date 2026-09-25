import { useRef, useState, type ReactNode } from "react";
import { formatDuration } from "../lib/format";
import { supervisorStale, underHost } from "../lib/hosts";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { runKey, type ProjectGroup } from "../lib/runs";
import type { Run } from "../lib/types";
import { ReasonAction } from "./ReasonAction";
import { RunRow } from "./RunRow";
import { SettingsSheet } from "./SettingsSheet";

/** One project's card: the daemon's host and supervisor line over a row
 *  per live run. The header's Settings entry opens the project's
 *  `SettingsSheet` over the page; closing it returns focus to the entry.
 *  Beside it, Hold for an `enabled` project, or the hold note and Release
 *  hold for a `held` one, each posting its reason to the daemon;
 *  `actionFetch` defaults to the page's own. */
export function ProjectBlock({
  group,
  expandedRun,
  onToggleRun,
  renderDetail,
  deps = defaultPollDeps,
  sinceMs = 0,
  actionFetch,
}: {
  group: ProjectGroup;
  expandedRun: string | null;
  onToggleRun: (key: string) => void;
  renderDetail?: (run: Run, group: ProjectGroup) => ReactNode;
  deps?: { fetch: Fetch };
  /** Local milliseconds since `group.status` arrived, handed to each row. */
  sinceMs?: number;
  actionFetch?: Fetch;
}) {
  const { status } = group;
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsButton = useRef<HTMLButtonElement>(null);
  const closeSettings = () => {
    setSettingsOpen(false);
    settingsButton.current?.focus();
  };
  const { supervisor, thresholds } = status;
  const daemon = { base: group.base, actions: status.actions === true, fetch: actionFetch };
  // Under a host daemon the beat is the host sweep's, judged by the daemon.
  const stale = supervisorStale(underHost(group.base), status);
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
        {(status.workers_on_previous_build ?? 0) > 0 && (
          <span className="text-[12px] text-faint">
            {status.workers_on_previous_build} {status.workers_on_previous_build === 1 ? "worker" : "workers"} on previous build
          </span>
        )}
        {Object.entries(status.active_routes ?? {}).filter(([, route]) => route.fallback).map(([seat, route]) => (
          <span key={seat} title={route.command ?? ""} className="rounded-chip border border-chip-border px-2 py-[2px] text-[12px] text-ink">
            {seat}: fallback ({/devin/i.test(route.command ?? "") ? "Devin" : /codex/i.test(route.command ?? "") ? "Codex" : route.command})
          </span>
        ))}
        {status.admission === "held" && (
          <span data-hold-note className="text-[12px] font-semibold text-bad">
            held{status.hold_note ? `: ${status.hold_note}` : ""}
          </span>
        )}
        {status.admission === "enabled" && <ReasonAction daemon={daemon} route="/actions/hold" body={{}} label="Hold" />}
        {status.admission === "held" && <ReasonAction daemon={daemon} route="/actions/release-hold" body={{}} label="Release hold" />}
        <button
          ref={settingsButton}
          type="button"
          aria-pressed={settingsOpen}
          onClick={() => setSettingsOpen(true)}
          className="rounded-button border border-chip-border px-2 py-[2px] text-[12px] font-semibold text-ink hover:bg-chip-border/40"
        >
          Settings
        </button>
      </header>
      <ul>
        {group.runs.map((run) => {
          const key = runKey(group.base, run.id);
          const expanded = expandedRun === key;
          return (
            <RunRow
              key={run.id}
              run={run}
              base={group.base}
              polls={status.now}
              deps={deps}
              thresholds={thresholds}
              expanded={expanded}
              onToggle={() => onToggleRun(key)}
              detail={expanded ? renderDetail?.(run, group) : undefined}
              sinceMs={sinceMs}
            />
          );
        })}
      </ul>
      {settingsOpen && (
        <SettingsSheet base={group.base} name={group.name} path={group.path} status={status} onClose={closeSettings} deps={deps} />
      )}
    </section>
  );
}
