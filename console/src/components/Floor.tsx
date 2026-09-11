import type { ReactNode } from "react";
import type { ProjectChoice } from "../lib/attention";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { groupByProject, type DaemonStatus, type ProjectGroup } from "../lib/runs";
import type { Run } from "../lib/types";
import { ProjectBlock } from "./ProjectBlock";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** What is running, under the needs-you band: one block per project and
 *  daemon in view with a row per live run. `expandedRun` is the Now view's
 *  `runKey`, and `renderDetail` fills the open row's slot. */
export function Floor({
  daemons,
  project,
  expandedRun,
  onToggleRun,
  renderDetail,
  deps = defaultPollDeps,
}: {
  daemons: DaemonStatus[];
  project: ProjectChoice;
  expandedRun: string | null;
  onToggleRun: (key: string) => void;
  renderDetail?: (run: Run, group: ProjectGroup) => ReactNode;
  deps?: { fetch: Fetch };
}) {
  const groups = groupByProject(daemons).filter((group) => project === "all" || group.path === project);
  const runs = groups.reduce((total, group) => total + group.runs.length, 0);
  const projects = new Set(groups.map((group) => group.path)).size;
  return (
    <section aria-label="Floor" className="px-6 pt-[18px] pb-6">
      <div className="flex items-baseline gap-3">
        <h2 className="text-[20px] font-semibold text-ink">Floor</h2>
        <span className="text-[13px] text-muted">
          {plural(runs, "run")} · {plural(projects, "project")}
        </span>
      </div>
      {runs === 0 ? (
        <p className="mt-3 text-[13px] text-muted">Nothing on the floor</p>
      ) : (
        <div className="mt-3 flex flex-col gap-3">
          {groups
            .filter((group) => group.runs.length > 0)
            .map((group) => (
              <ProjectBlock
                key={`${group.base} ${group.path}`}
                group={group}
                expandedRun={expandedRun}
                onToggleRun={onToggleRun}
                renderDetail={renderDetail}
                deps={deps}
              />
            ))}
        </div>
      )}
    </section>
  );
}
