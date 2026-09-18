import type { Ledgers } from "../hooks/useLedger";
import type { HostRecord } from "./hosts";
import { localMidnight, type LedgerRow } from "./ledger";

const SIX_HOURS = 6 * 60 * 60 * 1000;
const time = (at: number) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** Count stores, not migration events or daemon ports. Older daemons without
 * structured migration fields cannot contribute a schema summary. */
export function migrationLines(hosts: HostRecord[], ledgers: Ledgers, now: number): string[] {
  const groups = new Map<string, HostRecord[]>();
  for (const host of hosts) {
    const name = host.status?.host ?? host.address;
    groups.set(name, [...(groups.get(name) ?? []), host]);
  }
  const lines: string[] = [];
  for (const [name, siblings] of groups) {
    const latest = new Map<string, LedgerRow>();
    const migrated = new Set<string>();
    for (const host of siblings) {
      for (const row of ledgers[host.address]?.rows ?? []) {
        if (row.action !== "migrate" || row.at < localMidnight(now)) continue;
        const project = String(row.project ?? host.project ?? host.address);
        migrated.add(project);
        if (row.schema_to == null || row.schema_from == null) continue;
        if (!latest.has(project) || latest.get(project)!.at < row.at) latest.set(project, row);
      }
    }
    const versions = new Map<number, [string, LedgerRow][]>();
    for (const entry of latest) {
      if (now - entry[1].at >= SIX_HOURS) continue;
      const version = entry[1].schema_to!;
      versions.set(version, [...(versions.get(version) ?? []), entry]);
    }
    if (versions.size === 0) continue;
    const newest = Math.max(...versions.keys());
    for (const [version, entries] of versions) {
      const rows = entries.map(([, row]) => row);
      const origins = [...new Set(rows.map((row) => row.schema_from))].sort((a, b) => a! - b!);
      const start = Math.min(...rows.map((row) => row.at));
      const end = Math.max(...rows.map((row) => row.at));
      const projects = version < newest ? ` · ${entries.map(([project]) => project).join(", ")}` : "";
      lines.push(`${name} · ${rows.length} ${rows.length === 1 ? "store" : "stores"} at schema ${version}${projects} (migrated from ${origins.join(", ")} today ${time(start)} to ${time(end)})`);
    }
    for (const host of siblings) {
      const project = host.project ?? host.address;
      const version = host.status?.schema_version;
      if (!migrated.has(project) && version != null && version < newest) {
        lines.push(`${name} · ${project} still at schema ${version}`);
      }
    }
  }
  return lines;
}
