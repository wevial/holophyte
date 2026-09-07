import { useRef, useState } from "react";
import { useLedger } from "../hooks/useLedger";
import type { ProjectChoice } from "../lib/attention";
import { hostItems, visibleHosts, type HostRecord } from "../lib/hosts";
import { localMidnight } from "../lib/ledger";
import { defaultPollDeps, type Fetch } from "../lib/poll";
import { resolvedSince } from "../lib/resolved";
import type { DaemonStatus } from "../lib/runs";
import type { AttentionItem } from "../lib/types";
import { Floor } from "./Floor";
import { NeedsYou } from "./NeedsYou";
import { ResolvedFold } from "./ResolvedFold";
import { RunDetail } from "./RunDetail";

/** The key an attention item is remembered by once seen, so a resolving
 *  ledger row can still find what it cleared after the item left the band. */
function itemKey(item: AttentionItem): string {
  return `${String(item.daemon ?? "")}:${item.kind}:${String(item.run ?? "")}:${String(item.ticket ?? "")}`;
}

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
  for (const host of shown) if (host.status) daemons.push({ base: host.base, status: host.status });

  // Every attention item seen this session, so the fold can pair an
  // intervention with the item it cleared once the band has dropped it.
  const history = useRef(new Map<string, AttentionItem>());
  for (const host of shown) for (const item of hostItems(host, now)) history.current.set(itemKey(item), item);
  const ledgers = useLedger(shown, now, polls, deps);
  const served = shown.filter((host) => ledgers[host.address] && !ledgers[host.address]!.absent);
  const ledgerRows = served.flatMap((host) => ledgers[host.address]!.rows);
  const resolved = resolvedSince(ledgerRows, [...history.current.values()], localMidnight(now));
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
        renderDetail={(run, group) => (
          <RunDetail base={group.base} id={run.id} now={group.status.now} polls={polls} deps={deps} />
        )}
      />
    </>
  );
}
