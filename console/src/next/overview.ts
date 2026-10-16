import { useContext } from "react";
import { useSearch } from "@tanstack/react-router";
import { needsYouEntries, type NeedsYouEntry, type ReadinessMap } from "../lib/attention";
import type { QueuedTicket, Status } from "../lib/contract.gen";
import { hostItems, runCounts, type HostRecord } from "../lib/hosts";
import type { AttentionItem, ShippedRow } from "../lib/types";
import type { ContractBodies } from "./fleet";
import { useMergedToday, useReadiness } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";

export interface Overview {
  project: string | undefined;
  now: number;
  hosts: HostRecord<ContractBodies>[];
  /** Whether some scoped host has answered well at least once. */
  answered: boolean;
  items: AttentionItem[];
  needs: NeedsYouEntry[];
  readiness: ReadinessMap;
  pullRequests: AttentionItem[];
  runs: Status["runs"];
  running: number;
  queued: QueuedTicket[];
  mergedToday: ShippedRow[];
}

/** The scoped fleet every Overview section reads. */
export function useOverview(): Overview {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const { now } = peers;
  const hosts = scopedHosts(peers.hosts, project);
  const items = hosts.flatMap((host) => hostItems(host, now));
  const readiness = useReadiness(hosts, deps);
  const statuses = hosts.flatMap((host) => (host.status ? [host.status] : []));
  return {
    project,
    now,
    hosts,
    answered: hosts.some((host) => host.seen_ms != null),
    items,
    needs: needsYouEntries(items, readiness, now),
    readiness,
    pullRequests: items.filter((item) => item.kind === "pr_open"),
    runs: statuses.flatMap((status) => status.runs),
    running: statuses.reduce((sum, status) => sum + runCounts(status).active, 0),
    queued: statuses.flatMap((status) => status.queued),
    mergedToday: useMergedToday(hosts, now, deps.fetch),
  };
}
