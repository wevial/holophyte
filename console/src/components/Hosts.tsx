import type { ProjectChoice } from "../lib/attention";
import { byDaemon, groupByHost, hostName, visibleHosts, type HostRecord } from "../lib/hosts";
import { HostDaemonPanel } from "./HostDaemonPanel";
import { HostAgents, HostPanel } from "./HostPanel";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** "2 hosts · 2 daemons on :7710": distinct host names, daemon count
 *  (a host daemon's projects are one daemon), and the distinct ports the
 *  daemons bound. */
export function hostsSubtitle(hosts: HostRecord[]): string {
  const names = new Set(hosts.map(hostName));
  const daemons = new Set(hosts.map((host) => host.address));
  const ports = [...new Set(hosts.map((host) => /:\d+$/.exec(host.address)?.[0]).filter((port) => port != null))];
  const on = ports.length > 0 ? ` on ${ports.join(", ")}` : "";
  return `${plural(names.size, "host")} · ${plural(daemons.size, "daemon")}${on}`;
}

/** The Hosts view: one card per daemon in view, two to a row, each host's
 *  daemons followed by its Agents table across the row, the host's group
 *  spanning full rows so a host with no table never shares a row with the
 *  next. A host daemon is one full-width card listing its projects. */
export function Hosts({ hosts, project, now }: { hosts: HostRecord[]; project: ProjectChoice; now: number }) {
  const shown = visibleHosts(hosts, project);
  return (
    <section aria-label="Hosts & daemons" className="px-6 pt-6 pb-6">
      <div className="flex items-baseline gap-3">
        <h1 className="text-[20px] font-semibold text-ink">Hosts &amp; daemons</h1>
        {shown.length > 0 && (
          <span data-subtitle className="text-[13px] text-muted">
            {hostsSubtitle(shown)}
          </span>
        )}
      </div>
      {shown.length === 0 ? (
        <p className="mt-3 text-[13px] text-muted">No daemon answered yet</p>
      ) : (
        <div className="mt-4 grid grid-cols-[repeat(2,minmax(0,1fr))] gap-4">
          {groupByHost(shown).map((group) => (
            <div key={group.label} data-host-group={group.label} className="col-span-2 grid grid-cols-[repeat(2,minmax(0,1fr))] gap-4">
              {byDaemon(group.hosts).map((records) =>
                records.some((record) => record.name != null) ? (
                  <HostDaemonPanel key={records[0]!.address} records={records} now={now} />
                ) : (
                  <HostPanel key={records[0]!.key} host={records[0]!} now={now} />
                ),
              )}
              <HostAgents label={group.label} hosts={group.hosts} />
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
