import { projectName } from "../lib/derive";
import { hostItems, type HostRecord } from "../lib/hosts";
import type { PollBodies } from "../lib/poll";
import type { Status } from "../lib/types";

/** One row of the project switcher; `name` is null for "All projects". */
export interface SwitcherRow {
  name: string | null;
  label: string;
  held: boolean;
  need: number;
  open: number;
  down: boolean;
}

const ALL_PROJECTS = "All projects";

interface SwitcherBodies extends PollBodies {
  status: Pick<Status, "project" | "host" | "admission" | "runs">;
}

type SwitcherHost = HostRecord<SwitcherBodies>;

function rowOf(name: string | null, hosts: SwitcherHost[], now: number): SwitcherRow {
  return {
    name,
    label: name ?? ALL_PROJECTS,
    held: name != null && hosts.some((host) => host.status?.admission === "held"),
    need: hosts.reduce((total, host) => total + hostItems(host, now).length, 0),
    open: hosts.reduce((total, host) => total + (host.status?.runs.length ?? 0), 0),
    down: name != null && hosts.every((host) => host.error != null),
  };
}

/** "All projects", then one row per project name in the order the hosts
 *  first name them, pooling the hosts that serve the same name. */
export function switcherRows(hosts: SwitcherHost[], now: number): SwitcherRow[] {
  const byName = new Map<string, SwitcherHost[]>();
  for (const host of hosts) {
    if (host.project == null) continue;
    const name = projectName(host.project);
    byName.set(name, [...(byName.get(name) ?? []), host]);
  }
  return [rowOf(null, hosts, now), ...[...byName].map(([name, pooled]) => rowOf(name, pooled, now))];
}

export function matchRows(rows: SwitcherRow[], query: string): SwitcherRow[] {
  const needle = query.trim().toLowerCase();
  return rows.filter((row) => row.label.toLowerCase().includes(needle));
}

const SCOPE_KEY = "holophyte.next.project";

export function readStoredScope(): string | undefined {
  try {
    return sessionStorage.getItem(SCOPE_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

export function storeScope(project: string | null): void {
  try {
    if (project == null) sessionStorage.removeItem(SCOPE_KEY);
    else sessionStorage.setItem(SCOPE_KEY, project);
  } catch {
    // Storage may be disabled; the pick still holds in the link.
  }
}
