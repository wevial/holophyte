import type { Ledger } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { formatAge, formatClock } from "../lib/format";
import { addressOf, groupByHost, hostName, oldestPoll, rootOf, sinceSeen, SWEEP_OK, type HostRecord } from "../lib/hosts";
import { migrationLines } from "../lib/migrations";
import type { ContractBodies } from "./fleet";

type Host = HostRecord<ContractBodies>;

const SHORT_SHA = 7;
const short = (sha: string | null | undefined) => (sha == null ? "unknown" : sha.slice(0, SHORT_SHA));

export interface StaleConsole {
  title: string;
  builds: string;
  reason: string | null;
}

/** The banner for the console the page's own host daemon built, null
 *  unless that daemon calls it stale. */
export function staleConsole(hosts: Host[], origin: string): StaleConsole | null {
  const own = hosts.find((host) => rootOf(host.base) === origin && host.host_status != null);
  const built = own?.host_status?.console;
  if (!own || built?.stale !== true) return null;
  return {
    title: `This console is older than ${hostName(own)}'s daemon.`,
    builds: `console ${short(built.served)} · daemon ${short(own.host_status!.build.daemon)}`,
    reason: built.reason,
  };
}

/** One line per ongoing route outage and per store migration in each
 *  host's day ledger, keyed by the record it was read for. */
export function noticeLines(hosts: Host[], answers: Map<string, Ledger>, now: number): string[] {
  const outages = hosts.flatMap((host) =>
    (answers.get(host.key)?.active_outages ?? []).map(
      (row) => `${projectName(host.project ?? host.address)} · implementer route down since ${formatClock(row.at)} · ${row.reason}`,
    ),
  );
  const ledgers = Object.fromEntries(Array.from(answers, ([key, answer]) => [key, { rows: answer.entries }]));
  const served = hosts.filter((host) => answers.has(host.key));
  return [...outages, ...migrationLines(served, ledgers, now)];
}

export interface SilentHost {
  title: string;
  detail: string;
}

/** A banner per host whose every record failed its last poll. */
export function silentHosts(hosts: Host[], now: number): SilentHost[] {
  return groupByHost(hosts)
    .filter((group) => group.hosts.every((host) => host.error != null))
    .map((group) => {
      const seen = group.hosts.reduce<number | null>((newest, host) => (host.seen_ms == null ? newest : Math.max(newest ?? 0, host.seen_ms)), null);
      return {
        title: `${group.label} is not answering.`,
        detail:
          seen == null
            ? "It has not answered since this page opened."
            : `last seen ${formatAge(sinceSeen(seen, now))} ago · its rows below are from ${formatClock(seen)} and may be out of date`,
      };
    });
}

/** The line while no host has answered, null once one has; before
 *  `/peers` names any host it asks the page's own. */
export function firstLoadLine(hosts: Host[], origin: string): string | null {
  if (hosts.some((host) => host.seen_ms != null)) return null;
  const labels = hosts.length === 0 ? [addressOf(origin).replace(/:\d+$/, "")] : groupByHost(hosts).map((group) => group.label);
  return labels.length === 1 ? `Asking ${labels[0]} for its projects…` : `Asking ${labels.length} hosts for their projects…`;
}

export interface FooterLine {
  lead: string;
  tail: { text: string; bad: boolean } | null;
}

const answeringOf = (hosts: Host[]) => {
  const answering = hosts.filter((host) => host.status != null && host.error == null).length;
  if (answering === hosts.length) return `${hosts.length} ${hosts.length === 1 ? "project" : "projects"} answering`;
  return `${answering} of ${hosts.length} projects answering`;
};

function sweepPart(hosts: Host[], now: number): FooterLine["tail"] {
  const rooted = hosts.filter((host) => host.host_status != null);
  const host = rooted.reduce<Host | null>((newest, record) => ((newest?.seen_ms ?? -1) >= (record.seen_ms ?? -1) ? newest : record), null);
  if (!host) {
    const polled = oldestPoll(hosts);
    return polled == null ? null : { text: `polled ${formatAge(Math.max(0, now - polled))} ago`, bad: false };
  }
  const { sweep, now: daemonNow } = host.host_status!;
  if (!SWEEP_OK.has(sweep.state)) return { text: `sweep ${sweep.state}`, bad: true };
  if (sweep.ended == null) return { text: "sweep ok", bad: false };
  return { text: `sweep ok ${formatAge(daemonNow - sweep.ended + sinceSeen(host.seen_ms, now))} ago`, bad: false };
}

/** The sidebar's footer over every host, null before the first answer. */
export function footerLine(hosts: Host[], now: number): FooterLine | null {
  if (!hosts.some((host) => host.seen_ms != null)) return null;
  const groups = groupByHost(hosts);
  if (groups.length > 1) return { lead: `${groups.length} hosts · ${answeringOf(hosts)}`, tail: null };
  const [group] = groups;
  return { lead: `${group!.label} · ${answeringOf(group!.hosts)}`, tail: sweepPart(group!.hosts, now) };
}
