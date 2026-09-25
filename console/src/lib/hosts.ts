import type { ProjectChoice } from "./attention";
import { isSupervisorStale, isStale } from "./derive";
import type { PollFailure, ProjectAnswer } from "./poll";
import type { Attention, AttentionItem, HostStatus, Status } from "./types";

/** One project the console polls, at the base its routes hang off: a
 *  project daemon (the origin that served the page, then each address
 *  `/peers` names), or one registered project of a host daemon, whose
 *  routes answer under `/projects/NAME` of that daemon. A host daemon's
 *  projects are a record each, so every view keyed by `base` -- run
 *  detail, the Board, the ledger of finished runs -- tells two projects on
 *  one daemon apart; the host card groups them again by `address`. */
export interface HostRecord {
  /** The record's identity: the address for a project daemon, the
   *  address and `/projects/NAME` for a project behind a host daemon. */
  key: string;
  /** `HOST:PORT` as `/peers` names it; the origin's from its `self`. */
  address: string;
  /** The URL the project's endpoints hang off: the daemon's own, or the
   *  daemon's with `/projects/NAME` after it. */
  base: string;
  /** What the rail calls the daemon before `/status` names its host. */
  label: string;
  /** The project path, from its last good `/status` (under a host daemon
   *  the root's list names it first); null before the first answer. */
  project: string | null;
  /** Under a host daemon: the project's route name, its `[serve] name`;
   *  absent or null for a project daemon, and null for the one record a
   *  host daemon whose registry names no project stands on. */
  name?: string | null;
  /** Under a host daemon: the daemon's last good root `/status` and
   *  `/attention`, which every project record of it shares. */
  host_status?: HostStatus | null;
  host_attention?: Attention | null;
  /** Under a host daemon: the last poll's failure was the daemon's root,
   *  not this project alone. */
  root_failed?: boolean;
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
  /** Beside `needs_token`: a 401 answered a request that carried a saved
   *  token, so the token is wrong rather than missing. Kept through the
   *  bare 401s after it (the page forgets a refused token) until a good
   *  answer. */
  token_rejected?: boolean;
  contract_error?: boolean;
}

/** Where a host daemon's project routes hang off the daemon's base. The
 *  name is the registry's `[serve] name`, which holds no `/` but may hold
 *  a space: it goes on the path percent-encoded, and the daemon decodes
 *  the segment before it asks the registry. */
export const PROJECTS_PREFIX = "/projects/";

export function projectBase(root: string, name: string): string {
  return `${root}${PROJECTS_PREFIX}${encodeURIComponent(name)}`;
}

/** The daemon's own base for a record's `base`: a host daemon project's
 *  prefix dropped, a project daemon's base unchanged. Root routes such as
 *  `/actions/run-sweep` are posted here. */
export function rootOf(base: string): string {
  return base.replace(/\/projects\/[^/]+$/, "");
}

/** Whether `base` is a project's prefix on a host daemon. */
export function underHost(base: string): boolean {
  return rootOf(base) !== base;
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

/** One poll's answer for one address: a project daemon's two bodies, a
 *  host daemon's root answers with each project's own, or the failure of
 *  the daemon's root. */
export type PollResult =
  | { address: string; base: string; ok: true; status: Status; attention: Attention }
  | { address: string; base: string; ok: true; host: HostStatus; host_attention: Attention; projects: ProjectAnswer[] }
  | ({ address: string; base: string } & PollFailure);

/** The project a host serves, from its last good `/status`. */
export function hostProject(status: Status | null): string | null {
  return status ? status.project : null;
}

type Where = Pick<HostRecord, "key" | "address" | "base">;

/** A record for a good answer at `where`. */
function answered(where: Where, answer: { status: Status; attention: Attention }, now: number): HostRecord {
  return {
    ...where,
    label: where.address,
    project: hostProject(answer.status),
    status: answer.status,
    attention: answer.attention,
    polled_ms: now,
    seen_ms: now,
    error: null,
    needs_token: false,
    token_rejected: false,
  };
}

/** A record for a failed answer at `where`, keeping `before`'s last good
 *  bodies; a 401 is `needs_token` rather than an error. */
function failed(where: Where, failure: PollFailure, before: HostRecord | undefined, now: number): HostRecord {
  const needsToken = failure.status === 401;
  return {
    ...before,
    ...where,
    label: where.address,
    project: before?.project ?? null,
    status: before?.status ?? null,
    attention: before?.attention ?? null,
    polled_ms: now,
    seen_ms: before?.seen_ms ?? null,
    error: needsToken ? null : failure.error,
    needs_token: needsToken,
    token_rejected: needsToken && (failure.token_sent === true || before?.token_rejected === true),
    contract_error: failure.contract_error,
  };
}

/** Fold one poll's results into the host list, in the results' order and
 *  a host daemon's projects in its registry's order. A failed result keeps
 *  the previous record's last good `/status` and `/attention` beside the
 *  new error, so the rail can say "unreachable · last seen 40s ago"; a
 *  host daemon whose root failed keeps every project record it had, each
 *  with the error, and one project that failed under its prefix is that
 *  record's error alone; an address seen for the first time that fails is
 *  one record with no answer yet. A host daemon whose registry names no
 *  project -- none registered, or none whose config gives a name -- is one
 *  record of its own at the daemon's base, so its card, its sweep and its
 *  broken entries stay in view. A 401 is not a failure to reach the
 *  daemon: the record is marked `needs_token` with no error, and
 *  `token_rejected` when the request carried a saved token. */
export function mergeHosts(previous: HostRecord[], results: PollResult[], now: number): HostRecord[] {
  const byKey = new Map(previous.map((host) => [host.key, host]));
  return results.flatMap((result): HostRecord[] => {
    const { address, base } = result;
    if (!result.ok) {
      const had = previous.filter((host) => host.address === address);
      if (had.length === 0) return [failed({ key: address, address, base }, result, undefined, now)];
      return had.map((before) => ({ ...failed({ key: before.key, address, base: before.base }, result, before, now), root_failed: before.host_status != null }));
    }
    if (!("host" in result)) return [answered({ key: address, address, base }, result, now)];
    const root = { host_status: result.host, host_attention: result.host_attention, root_failed: false };
    if (result.projects.length === 0) {
      return [{
        key: address, address, base, label: address, name: null, project: null, status: null, attention: null,
        polled_ms: now, seen_ms: now, error: null, needs_token: false, token_rejected: false, ...root,
      }];
    }
    return result.projects.map((project) => {
      const where = { key: projectBase(address, project.name), address, base: projectBase(base, project.name) };
      const record = project.ok ? answered(where, project, now) : failed(where, project, byKey.get(where.key), now);
      return { ...record, name: project.name, project: record.project ?? project.path, ...root };
    });
  });
}

/** The hosts a view shows: every one under "All projects", else the ones
 *  serving the selected project. A host that never answered has no
 *  project and shows only under "All projects". */
export function visibleHosts(hosts: HostRecord[], project: ProjectChoice): HostRecord[] {
  return project === "all" ? hosts : hosts.filter((host) => host.project === project);
}

/** The name the page shows for a host: what `/status` calls it (a host
 *  daemon's root, before a project has answered, by its first project's
 *  label), else the host part of its address. */
export function hostName(host: HostRecord): string {
  const rootLabel = host.host_status?.projects.find((project) => project.host != null)?.host;
  return host.status?.host ?? rootLabel ?? host.address.replace(/:\d+$/, "");
}

/** The daemons under one host label: the card the rail draws. */
export interface HostGroup {
  /** What `/status` calls the host, else the host part of the address. */
  label: string;
  /** The records on that host, in poll order. */
  hosts: HostRecord[];
}

/** Host records grouped by their label, in the order each label is first
 *  seen; a daemon that never answered groups under its address's host.
 *  Every project of one host daemon lands in the group its first project
 *  opened, whatever label a later one reports: one daemon, one card. */
export function groupByHost(hosts: HostRecord[]): HostGroup[] {
  const groups: HostGroup[] = [];
  const byAddress = new Map<string, HostGroup>();
  for (const host of hosts) {
    const label = hostName(host);
    let group = byAddress.get(host.address) ?? groups.find((candidate) => candidate.label === label);
    if (group) group.hosts.push(host);
    else {
      group = { label, hosts: [host] };
      groups.push(group);
    }
    byAddress.set(host.address, group);
  }
  return groups;
}

/** A host group's records split by the daemon that serves them: a
 *  project daemon's record alone, a host daemon's projects together, in
 *  the order each daemon is first seen. */
export function byDaemon(hosts: HostRecord[]): HostRecord[][] {
  const daemons = new Map<string, HostRecord[]>();
  for (const host of hosts) daemons.set(host.address, [...(daemons.get(host.address) ?? []), host]);
  return [...daemons.values()];
}

/** The foot of a host card: `daemons up 12h` when every answered daemon
 *  started within a minute of the others, else the shortest uptime; null
 *  when none has said when it started. Each uptime is read against its
 *  own daemon's clock. */
export function daemonsUp(hosts: HostRecord[]): number | null {
  const uptimes = hosts.flatMap((host) => (host.status?.daemon ? [host.status.now - host.status.daemon.started_ms] : []));
  return uptimes.length === 0 ? null : Math.min(...uptimes);
}

/** A host's dot and border: bad when unreachable or its supervisor is
 *  stale, ok when the supervisor is live, faint otherwise, including
 *  while the daemon waits for its token. */
export type HostTone = "ok" | "bad" | "faint";

export function hostTone(host: HostRecord): HostTone {
  if (host.error != null) return "bad";
  if (host.needs_token) return "faint";
  if (!host.status) return "faint";
  if (supervisorStale(host.name != null, host.status)) return "bad";
  return host.status.supervisor.state === "live" ? "ok" : "faint";
}

/** Whether `status`'s supervisor is stale. Under a host daemon (`onHost`)
 *  the beat is the host sweep's, which the daemon judges against two sweep
 *  intervals, not the runs' `heartbeat_stale_ms`: its `state` is the word. */
export function supervisorStale(onHost: boolean, status: Status): boolean {
  if (onHost) return status.supervisor.state === "stale";
  return isSupervisorStale(status.supervisor, status.thresholds.heartbeat_stale_ms);
}

/** The `/attention` kind the console adds for a daemon it cannot reach. */
export const UNREACHABLE = "unreachable";

/** The sweep states a host daemon's root `/attention` leaves alone
 *  (holophyte/serve_host.py `SWEEP_OK`). */
export const SWEEP_OK = new Set(["fresh", "running"]);

/** Whether `host` is the record a host daemon's own items ride on: its
 *  first project the registry names, else the daemon's own record. */
function carriesHostItems(host: HostRecord): boolean {
  if (host.host_status == null) return false;
  const first = host.host_status.projects.find((project) => project.name != null);
  return (first?.name ?? null) === (host.name ?? null);
}

/** The host daemon's own rows: root `/attention`'s `sweep_stale` as a
 *  `supervisor` item, since on a host the sweep is the supervisor, and an
 *  `unreachable` item for each registered project whose config gives no
 *  name, which no prefix routes to and no record stands for. */
function rootItems(host: HostRecord, project: string, now: number): AttentionItem[] {
  if (!carriesHostItems(host)) return [];
  const sweep = (host.host_attention?.items ?? [])
    .filter((item) => item.kind === "sweep_stale")
    .map((item) => ({
      kind: "supervisor",
      level: item.level,
      host_sweep: true,
      sweep_state: item.state,
      ended_ms: item.ended,
      project,
      daemon: host.key,
      host: hostName(host),
    }));
  const unnamed = (host.host_status?.projects ?? [])
    .filter((entry) => entry.name == null)
    .map((entry) => ({
      kind: UNREACHABLE,
      level: "critical",
      daemon: host.key,
      project: entry.path,
      host: `${entry.path} on ${hostName(host)}`,
      error: entry.error ?? "its config gives no [serve] name",
      last_seen_ms: null,
      asked_ms: host.seen_ms ?? now,
    }));
  return [...unnamed, ...sweep];
}

/** One host's attention items, each stamped with the record's key and
 *  project, plus one critical `unreachable` item when the last poll failed
 *  and, on the record a host daemon's own rows ride on, the host sweep's
 *  row when it is not fresh and a row per project with no name. A
 *  `supervisor` item under a host daemon is the sweep's: `host_sweep`
 *  marks it for "Run sweep". `now` is the console's clock, for the
 *  unreachable item's `since_ms`. */
export function hostItems(host: HostRecord, now: number): AttentionItem[] {
  const project = host.project ?? host.address;
  const onHost = host.name != null;
  const items: AttentionItem[] = (host.attention?.items ?? []).map((item) => ({
    ...item,
    project: item.project ?? project,
    daemon: host.key,
    ...(onHost && item.kind === "supervisor" ? { host_sweep: true } : {}),
  }));
  items.push(...rootItems(host, project, now));
  if (host.error != null) {
    items.unshift({
      kind: UNREACHABLE,
      level: "critical",
      daemon: host.key,
      project,
      host: onHost ? `${host.name} on ${hostName(host)}` : hostName(host),
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

/** A host card's Toil cell: `24h 0.50 · 7d 1.25`, each window's
 *  interventions per merged run, `—` for a window with no merges; and the
 *  week's two most frequent actions, ties by name, as `requeue 5 · babysit
 *  3`, null when the week has none. */
export function toilLines(toil: NonNullable<Status["toil"]>): { rates: string; actions: string | null } {
  const rate = (perMerge: number | null) => (perMerge == null ? "—" : perMerge.toFixed(2));
  const top = Object.entries(toil["7d"].by_action)
    .sort(([a, x], [b, y]) => y - x || (a < b ? -1 : a > b ? 1 : 0))
    .slice(0, 2)
    .map(([action, count]) => `${action} ${count}`);
  return {
    rates: `24h ${rate(toil["24h"].per_merge)} · 7d ${rate(toil["7d"].per_merge)}`,
    actions: top.length === 0 ? null : top.join(" · "),
  };
}

/** The oldest `polled_ms` across hosts, null before the first poll. */
export function oldestPoll(hosts: HostRecord[]): number | null {
  if (hosts.length === 0) return null;
  return hosts.reduce((least, host) => Math.min(least, host.polled_ms), Number.POSITIVE_INFINITY);
}

/** Local milliseconds since a daemon's answer last arrived (`seen_ms`);
 *  the ages it computed then plus this read as their ages right now.
 *  0 for a record that never said when it was seen. */
export function sinceSeen(seenMs: number | null | undefined, now: number): number {
  return seenMs ? Math.max(0, now - seenMs) : 0;
}
