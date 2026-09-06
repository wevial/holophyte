import { useState } from "react";
import type { ProjectChoice } from "../lib/attention";
import { visibleHosts, type HostRecord } from "../lib/hosts";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { Status } from "../lib/types";
import { Floor } from "./Floor";
import { NeedsYou } from "./NeedsYou";
import { RunDetail } from "./RunDetail";

/** The Now view: the needs-you band over the Floor, both narrowed to the
 *  hosts serving `project`. The one expanded run lives here and its detail
 *  card reads `/runs/N` from the run's own daemon, refreshed each time
 *  `polls` advances. `now` is the console's clock. */
export function Now({
  hosts,
  project,
  now,
  polls = 0,
  deps = defaultPollDeps,
}: {
  hosts: HostRecord[];
  project: ProjectChoice;
  now: number;
  polls?: number;
  deps?: { fetch: Fetch };
}) {
  const [expandedRun, setExpandedRun] = useState<number | null>(null);
  const toggleRun = (id: number) => setExpandedRun((previous) => (previous === id ? null : id));
  const shown = visibleHosts(hosts, project);
  const statuses = shown.map((host) => host.status).filter((status): status is Status => status != null);
  const baseFor = (status: Status) => shown.find((host) => host.status === status)?.base ?? "";
  return (
    <>
      <NeedsYou hosts={shown} project={project} now={now} />
      <Floor
        statuses={statuses}
        project={project}
        expandedRun={expandedRun}
        onToggleRun={toggleRun}
        renderDetail={(run, status) => (
          <RunDetail base={baseFor(status)} id={run.id} now={status.now} polls={polls} deps={deps} />
        )}
      />
    </>
  );
}
