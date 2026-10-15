import { createContext, useContext } from "react";
import type { PeersState } from "../hooks/usePeers";
import type { PollBodies } from "../lib/poll";
import { projectName } from "../lib/derive";
import type { HostRecord } from "../lib/hosts";
import type { ContractBodies } from "./fleet";

/** The search every route of the new console inherits from its root. */
interface ScopeSearch {
  project?: string;
}

const PROJECT_MAX = 200;

export function validateScope(search: Record<string, unknown>): ScopeSearch {
  const { project } = search;
  return typeof project === "string" && project.length > 0 && project.length <= PROJECT_MAX ? { project } : {};
}

export const keepScope = (previous: ScopeSearch): ScopeSearch => ({ project: previous.project });

/** Every host with no scope, else the hosts whose project bears that name;
 *  a host that never answered has no project and so no name to match. */
export function scopedHosts<R extends HostRecord<PollBodies>>(hosts: R[], project: string | undefined): R[] {
  if (project == null) return hosts;
  return hosts.filter((host) => host.project != null && projectName(host.project) === project);
}

export const PeersContext = createContext<PeersState<ContractBodies> | null>(null);

/** The one `useFleet()` poll the console's root shares with every view. */
export function useConsolePeers(): PeersState<ContractBodies> {
  const peers = useContext(PeersContext);
  if (!peers) throw new Error("console: useConsolePeers outside the console's root");
  return peers;
}
