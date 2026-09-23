import type { ReactNode } from "react";
import { useTick } from "../hooks/useTick";
import type { ProjectChoice } from "../lib/attention";
import { sinceSeen } from "../lib/hosts";
import { defaultPollDeps, TICK_MS, type Fetch } from "../lib/poll";
import { groupByProject, type DaemonStatus, type ProjectGroup } from "../lib/runs";
import type { Run } from "../lib/types";
import { ProjectBlock } from "./ProjectBlock";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** What is running, under the needs-you band: one block per project and
 *  daemon in view with a row per live run. An idle project whose daemon
 *  reports its `admission` keeps its block, so Hold and Release hold stay
 *  in reach with no run on the floor. `expandedRun` is the Now view's
 *  `runKey`, and `renderDetail` fills the open row's slot. */
export function Floor({
  daemons,
  contractErrors = [],
  project,
  expandedRun,
  onToggleRun,
  renderDetail,
  deps = defaultPollDeps,
}: {
  daemons: DaemonStatus[];
  contractErrors?: string[];
  project: ProjectChoice;
  expandedRun: string | null;
  onToggleRun: (key: string) => void;
  renderDetail?: (run: Run, group: ProjectGroup) => ReactNode;
  deps?: { fetch: Fetch };
}) {
  const localNow = useTick(TICK_MS);
  const groups = groupByProject(daemons).filter((group) => project === "all" || group.path === project);
  const runs = groups.reduce((total, group) => total + group.runs.length, 0);
  const projects = new Set(groups.map((group) => group.path)).size;
  const blocks = groups.filter((group) =>
    group.runs.length > 0 || group.status.admission === "enabled" || group.status.admission === "held");
  return (
    <section aria-label="Floor" className="px-6 pt-[18px] pb-6">
      <div className="flex items-baseline gap-3">
        <h2 className="text-[20px] font-semibold text-ink">Floor</h2>
        <span className="text-[13px] text-muted">
          {plural(runs, "run")} · {plural(projects, "project")}
        </span>
      </div>
      {contractErrors.map((error) => <p role="alert" key={error} className="mt-3 text-sm font-semibold text-bad">{error}</p>)}
      {runs === 0 && (
        <p className="mt-3 text-[13px] text-muted">{contractErrors.length ? "Floor data unavailable" : "Nothing on the floor"}</p>
      )}
      {blocks.length > 0 && (
        <div className="mt-3 flex flex-col gap-3">
          {blocks.map((group) => (
            <ProjectBlock
              key={`${group.base} ${group.path}`}
              group={group}
              expandedRun={expandedRun}
              onToggleRun={onToggleRun}
              renderDetail={renderDetail}
              deps={deps}
              sinceMs={sinceSeen(group.seen_ms, localNow)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
