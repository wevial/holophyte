import { groupByHost, supervisorStale, type HostRecord } from "../lib/hosts";
import type { ContractBodies } from "./fleet";
import { scopedHosts } from "./scope";

export const SETTINGS_TABS = [
  { id: "general", label: "General", group: "Project" },
  { id: "review", label: "Review", group: "Project" },
  { id: "agents", label: "Agents", group: "Project" },
  { id: "capture", label: "Capture", group: "Project" },
  { id: "hold", label: "Hold / release", group: "Project" },
  { id: "toml", label: "Raw TOML", group: "Project" },
  { id: "hosts", label: "Hosts", group: "Factory" },
  { id: "appearance", label: "Appearance", group: "You · this browser" },
] as const;

type SettingsHost = HostRecord<ContractBodies>;

export function settingsHost<R extends SettingsHost>(hosts: R[], project: string | undefined): R | null {
  return scopedHosts(hosts, project).find((host) => host.project != null) ?? null;
}

export function hostsNeedingAttention(hosts: SettingsHost[]): number {
  return groupByHost(hosts).filter((group) => group.hosts.some((host) =>
    host.error != null || (host.status != null && supervisorStale(host.name != null, host.status)),
  )).length;
}
