import { useState } from "react";
import type { ProjectChoice } from "../lib/attention";
import { visibleHosts, type HostRecord } from "../lib/hosts";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import type { DaemonStatus } from "../lib/runs";
import { Floor } from "./Floor";
import { NeedsYou } from "./NeedsYou";
import { RunDetail } from "./RunDetail";

/** The Now view: the needs-you band over the Floor, both narrowed to the
 *  hosts serving `project`. The one expanded run lives here, keyed by its
 *  daemon and id so run #N on two daemons is two rows, and its detail
 *  card reads `/runs/N` from that daemon, refreshed each time `polls`
 *  advances. `now` is the console's clock. */
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
  const [expandedRun, setExpandedRun] = useState<string | null>(null);
  const toggleRun = (key: string) => setExpandedRun((previous) => (previous === key ? null : key));
  const shown = visibleHosts(hosts, project);
  const daemons: DaemonStatus[] = [];
  for (const host of shown) if (host.status) daemons.push({ base: host.base, status: host.status });
  return (
    <>
      <NeedsYou hosts={shown} project={project} now={now} />
      <Floor
        daemons={daemons}
        project={project}
        expandedRun={expandedRun}
        onToggleRun={toggleRun}
        renderDetail={(run, group) => (
          <RunDetail base={group.base} id={run.id} now={group.status.now} polls={polls} deps={deps} />
        )}
      />
    </>
  );
}
