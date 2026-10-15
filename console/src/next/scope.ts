import { createContext, useContext } from "react";
import type { PeersState } from "../hooks/usePeers";
import { projectName } from "../lib/derive";
import type { HostRecord } from "../lib/hosts";

/** The search every route of the new console inherits from its root. */
interface ScopeSearch {
  project?: string;
}

const PROJECT_MAX = 200;

export function validateScope(search: Record<string, unknown>): ScopeSearch {
  const { project } = search;
  return typeof project === "string" && project.length > 0 && project.length <= PROJECT_MAX ? { project } : {};
}

/** Every host with no scope, else the hosts whose project bears that name;
 *  a host that never answered has no project and so no name to match. */
export function scopedHosts(hosts: HostRecord[], project: string | undefined): HostRecord[] {
  if (project == null) return hosts;
  return hosts.filter((host) => host.project != null && projectName(host.project) === project);
}

export const PeersContext = createContext<PeersState | null>(null);

/** The one `usePeers()` poll the console's root shares with every view. */
export function useConsolePeers(): PeersState {
  const peers = useContext(PeersContext);
  if (!peers) throw new Error("console: useConsolePeers outside the console's root");
  return peers;
}
