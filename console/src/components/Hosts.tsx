import type { ProjectChoice } from "../lib/attention";
import { hostName, visibleHosts, type HostRecord } from "../lib/hosts";
import { HostPanel } from "./HostPanel";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** "2 hosts · 2 daemons on :7710": distinct host names, daemon count, and
 *  the distinct ports the daemons bound. */
export function hostsSubtitle(hosts: HostRecord[]): string {
  const names = new Set(hosts.map(hostName));
  const ports = [...new Set(hosts.map((host) => /:\d+$/.exec(host.address)?.[0]).filter((port) => port != null))];
  const on = ports.length > 0 ? ` on ${ports.join(", ")}` : "";
  return `${plural(names.size, "host")} · ${plural(hosts.length, "daemon")}${on}`;
}

/** The Hosts view: one card per daemon in view, two to a row. */
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
          {shown.map((host) => (
            <HostPanel key={host.address} host={host} now={now} />
          ))}
        </div>
      )}
    </section>
  );
}
