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

export type SettingsField = {
  tab: (typeof SETTINGS_TABS)[number]["id"];
  table: string;
  key: string;
  label: string;
  help: string;
} & (
  | { kind: "select"; default: string; options: readonly { value: string; label: string }[] }
  | { kind: "number"; default: number; numeric: "integer"; min: number }
);

export const SETTINGS_FIELDS: readonly SettingsField[] = [
  {
    tab: "general", table: "merge", key: "mode", label: "Merge mode",
    help: "Direct lands on main after review. Pull request opens one and waits for its checks.",
    default: "local", kind: "select",
    options: [{ value: "local", label: "Direct to main" }, { value: "pr", label: "Pull request" }],
  },
  {
    tab: "general", table: "merge", key: "approve", label: "Approve",
    help: "Auto merges once review passes and checks are green. Human waits in Needs you for your merge.",
    default: "auto", kind: "select",
    options: [{ value: "auto", label: "Auto" }, { value: "human", label: "Human" }],
  },
  {
    tab: "general", table: "loop", key: "workers", label: "Workers",
    help: "Tickets this project runs at once on {host}.",
    default: 1, kind: "number", numeric: "integer", min: 1,
  },
];
