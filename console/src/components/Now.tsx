import { useState } from "react";
import { useLedger } from "../hooks/useLedger";
import type { ProjectChoice } from "../lib/attention";
import { sinceSeen, visibleHosts, type HostRecord } from "../lib/hosts";
import { localMidnight } from "../lib/ledger";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { resolvedSince } from "../lib/resolved";
import type { DaemonStatus } from "../lib/runs";
import { Floor } from "./Floor";
import { NeedsYou } from "./NeedsYou";
import { ResolvedFold } from "./ResolvedFold";
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
  const [resolvedOpen, setResolvedOpen] = useState(false);
  const toggleRun = (key: string) => setExpandedRun((previous) => (previous === key ? null : key));
  const shown = visibleHosts(hosts, project);
  const daemons: DaemonStatus[] = [];
  for (const host of shown) if (host.status) daemons.push({ base: host.base, status: host.status, seen_ms: host.seen_ms });

  const ledgers = useLedger(shown, now, polls, deps);
  const served = shown.filter((host) => ledgers[host.address] && !ledgers[host.address]!.absent);
  const midnight = localMidnight(now);
  const resolved = served.flatMap((host) => resolvedSince(ledgers[host.address]!.rows, midnight)).sort((a, b) => b.at - a.at);
  return (
    <>
      <NeedsYou hosts={shown} project={project} now={now} ledgers={ledgers} />
      {served.length > 0 && (
        <ResolvedFold rows={resolved} open={resolvedOpen} onToggle={() => setResolvedOpen((previous) => !previous)} />
      )}
      <Floor
        daemons={daemons}
        project={project}
        expandedRun={expandedRun}
        onToggleRun={toggleRun}
        deps={deps}
        renderDetail={(run, group) => (
          <RunDetail
            base={group.base}
            id={run.id}
            now={group.status.now}
            sinceMs={sinceSeen(group.seen_ms, now)}
            polls={polls}
            deps={deps}
          />
        )}
      />
    </>
  );
}
