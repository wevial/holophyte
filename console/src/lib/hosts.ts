import type { ProjectChoice } from "./attention";
import { isSupervisorStale, isStale } from "./derive";
import type { Attention, AttentionItem, Status } from "./types";

/** One daemon the console polls: the origin that served the page first,
 *  then each address `/peers` names. */
export interface HostRecord {
  /** `HOST:PORT` as `/peers` names it; the origin's from its `self`. */
  address: string;
  /** The URL the daemon's endpoints hang off. */
  base: string;
  /** What the rail calls the daemon before `/status` names its host. */
  label: string;
  /** The project the daemon serves, from its last good `/status`; null
   *  before the first good answer. */
  project: string | null;
  /** The last good `/status`, kept beside a later error. */
  status: Status | null;
  /** The last good `/attention`, kept beside a later error. */
  attention: Attention | null;
  /** Clock reading when this record was last written, good or bad. */
  polled_ms: number;
  /** Clock reading of the last good answer, null before the first. */
  seen_ms: number | null;
  /** The last poll's failure or timeout, null after a good answer or
   *  when the daemon asked for a token instead. */
  error: string | null;
  /** The last poll answered 401: the daemon is up but wants its serve
   *  token, which the Hosts card asks for. Never set beside `error`. */
  needs_token: boolean;
}

/** What `/peers` answers: the daemon's own address and the configured
 *  list. A body naming the list `daemons` (the ticket's spelling) reads
 *  the same. */
export interface PeersBody {
  self?: string;
  peers?: string[];
  daemons?: string[];
}

/** The addresses to poll: the origin first, then each peer once, the
 *  origin's own address never repeated. */
export function peerAddresses(origin: string, body: PeersBody | null): { address: string; base: string }[] {
  const self = body?.self ?? addressOf(origin);
  const seen = new Set<string>([self]);
  const hosts = [{ address: self, base: origin }];
  for (const address of body?.peers ?? body?.daemons ?? []) {
    if (seen.has(address)) continue;
    seen.add(address);
    hosts.push({ address, base: baseOf(address) });
  }
  return hosts;
}

/** `HOST:PORT` of a base URL, the bare host when the URL carries no port. */
export function addressOf(base: string): string {
  try {
    return new URL(base).host;
  } catch {
    return base;
  }
}

/** The endpoint base for a `/peers` address: `http://HOST:PORT`, or the
 *  address itself when it already names a scheme. */
export function baseOf(address: string): string {
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(address) ? address.replace(/\/+$/, "") : `http://${address}`;
}

/** One poll's answer for one address. */
export type PollResult =
  | { address: string; base: string; ok: true; status: Status; attention: Attention }
  | { address: string; base: string; ok: false; error: string; status?: number };

/** The project a host serves, from its last good `/status`. */
export function hostProject(status: Status | null): string | null {
  return status ? (status.project ?? status.target) : null;
}

/** Fold one poll's results into the host list, in the results' order. A
 *  failed result keeps the previous record's last good `/status` and
 *  `/attention` beside the new error, so the rail can say "unreachable ·
 *  last seen 40s ago"; an address seen for the first time that fails is a
 *  record with no answer yet. A 401 is not a failure to reach the daemon:
 *  the record is marked `needs_token` with no error. */
export function mergeHosts(previous: HostRecord[], results: PollResult[], now: number): HostRecord[] {
  const byAddress = new Map(previous.map((host) => [host.address, host]));
  return results.map((result) => {
    const before = byAddress.get(result.address);
    if (result.ok) {
      return {
        address: result.address,
        base: result.base,
        label: result.address,
        project: hostProject(result.status),
        status: result.status,
        attention: result.attention,
        polled_ms: now,
        seen_ms: now,
        error: null,
        needs_token: false,
      };
    }
    const needsToken = result.status === 401;
    return {
      address: result.address,
      base: result.base,
      label: result.address,
      project: before?.project ?? null,
      status: before?.status ?? null,
      attention: before?.attention ?? null,
      polled_ms: now,
      seen_ms: before?.seen_ms ?? null,
      error: needsToken ? null : result.error,
      needs_token: needsToken,
    };
  });
}

/** The hosts a view shows: every one under "All projects", else the ones
 *  serving the selected project. A host that never answered has no
 *  project and shows only under "All projects". */
export function visibleHosts(hosts: HostRecord[], project: ProjectChoice): HostRecord[] {
  return project === "all" ? hosts : hosts.filter((host) => host.project === project);
}

/** The name the page shows for a host: what `/status` calls it, else the
 *  host part of its address. */
export function hostName(host: HostRecord): string {
  return host.status?.host ?? host.address.replace(/:\d+$/, "");
}

/** A host's dot and border: bad when unreachable or its supervisor is
 *  stale, ok when the supervisor is live, faint otherwise, including
 *  while the daemon waits for its token. */
export type HostTone = "ok" | "bad" | "faint";

export function hostTone(host: HostRecord): HostTone {
  if (host.error != null) return "bad";
  if (host.needs_token) return "faint";
  if (!host.status) return "faint";
  if (isSupervisorStale(host.status.supervisor, host.status.thresholds.heartbeat_stale_ms)) return "bad";
  return host.status.supervisor.state === "live" ? "ok" : "faint";
}

/** The `/attention` kind the console adds for a daemon it cannot reach. */
export const UNREACHABLE = "unreachable";

/** One host's attention items, each stamped with the host's address and
 *  project, plus one critical `unreachable` item when the last poll failed.
 *  `now` is the console's clock, for the item's `since_ms`. */
export function hostItems(host: HostRecord, now: number): AttentionItem[] {
  const project = host.project ?? host.address;
  const items: AttentionItem[] = (host.attention?.items ?? []).map((item) => ({
    ...item,
    project: item.project ?? project,
    daemon: host.address,
  }));
  if (host.error != null) {
    items.unshift({
      kind: UNREACHABLE,
      level: "critical",
      daemon: host.address,
      project,
      host: hostName(host),
      error: host.error,
      last_seen_ms: host.seen_ms,
      asked_ms: host.seen_ms ?? now,
    });
  }
  return items;
}

/** Live and stale run counts for a host's card. */
export function runCounts(status: Status): { active: number; stale: number } {
  const threshold = status.thresholds.heartbeat_stale_ms;
  const stale = status.runs.filter((run) => isStale(run.heartbeat_age_ms, threshold)).length;
  return { active: status.runs.length - stale, stale };
}

/** The oldest `polled_ms` across hosts, null before the first poll. */
export function oldestPoll(hosts: HostRecord[]): number | null {
  if (hosts.length === 0) return null;
  return hosts.reduce((least, host) => Math.min(least, host.polled_ms), Number.POSITIVE_INFINITY);
}
