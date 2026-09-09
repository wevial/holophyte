/**
 * The tray menu's summary as data: the SwiftBar drawer's rendering rules
 * (`contrib/swiftbar/holophyte.10s.py`) over the same `/status` and
 * `/attention` answers, folded into one menu. Pure: no Electron import and
 * no I/O, so the shape is testable from the JSON fixtures in
 * `docs/reference/http.md`; `main.ts` turns the items into a real menu and
 * picks the tray image from the level.
 *
 * Sections, top to bottom: what needs the operator ("Nothing needs you"
 * when nothing does), one line per project, the hosts footer, then the
 * fixed entries from `menu.ts`. Every age is rendered from the daemon's
 * own `now` and `_ms` fields, never from this machine's clock, so two
 * renders of the same answer are identical.
 */
import { type MenuActions, type MenuState, type TrayMenuItem, menuTemplate } from "./menu.ts";

export type Level = "idle" | "working" | "attention" | "bad";
const RANK: Record<Level, number> = { idle: 0, working: 1, attention: 2, bad: 3 };
const worse = (a: Level, b: Level): Level => (RANK[a] >= RANK[b] ? a : b);
/** `/attention`'s `level` word to the tray's. */
const DAEMON_LEVEL: Record<string, Level> = {
  none: "idle",
  working: "working",
  attention: "attention",
  critical: "bad",
};

/** One poll's answer for one path on one daemon. A 401 is not a failure to
 *  reach the daemon: it is up and wants the token `console.json` does not
 *  carry for it. Any other non-2xx keeps its status (a 404 on `/attention`
 *  is a daemon older than the path; a 503 is a target with no store yet)
 *  and its JSON body when there was one. */
export type FetchResult<T> =
  | { ok: true; body: T }
  | { ok: false; kind: "unauthorized" }
  | { ok: false; kind: "http"; status: number; body?: unknown }
  | { ok: false; kind: "unreachable"; error: string };

export type Run = {
  ticket: string;
  phase: string;
  heartbeat_age_ms?: number | null;
};
export type Status = {
  project?: string;
  target?: string;
  host?: string;
  now?: number;
  error?: string;
  supervisor?: { state?: string; heartbeat_age_ms?: number | null } | null;
  thresholds?: { heartbeat_stale_ms?: number | null };
  runs?: Run[];
};
export type AttentionItem = {
  kind?: string;
  ticket?: string;
  level?: string;
  question?: string;
  reason?: string;
  state?: string;
  asked_ms?: number | null;
  ended_ms?: number | null;
  attempt?: number;
  heartbeat_age_ms?: number | null;
};
export type Attention = { level?: string; now?: number; items?: AttentionItem[] };
export type Runs = { rows?: { ticket: string; outcome?: string; ended_ms?: number | null }[] };

/** A menu line, with the level its colour would carry in the drawer. */
type Row = { label: string; level: Level };

export type SummaryOptions = {
  /** `/runs` answers for the idle daemons, keyed by address, so the idle
   *  line can name the last merge as the drawer does. */
  runs?: Record<string, FetchResult<Runs>>;
  state?: MenuState;
  actions?: MenuActions;
};

export type Summary = { items: TrayMenuItem[]; level: Level };

/** `4s`, `12m`, `1h02m`; `?` when the daemon carried no number. */
export function age(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "?";
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/** An age as the daemon's own tables print one: `12s`, `7m`, `2h`, `3d`. */
export function coarseAge(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "?";
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

const QUESTION_CHARS = 60;
const REASON_CHARS = 40;

/** `text` on one line, at most `limit` characters, an ellipsis when cut. */
function cut(text: unknown, limit: number): string {
  const flat = String(text ?? "")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 1).trimEnd()}…`;
}

/** What the tray calls a daemon: the last path segment of its project,
 *  the address before `/status` has named one. */
export function projectName(address: string, status: Status | null): string {
  const project = status?.project ?? status?.target;
  if (!project) return address;
  const segments = project.split("/").filter(Boolean);
  return segments[segments.length - 1] ?? project;
}

/** The "needs you" text for one `/attention` item, or null for a kind this
 *  tray does not know (a newer daemon's item is skipped, not misrendered). */
function itemRow(name: string, item: AttentionItem, now: number | undefined): string | null {
  const { kind, ticket } = item;
  if (kind === "blocked") {
    const asked =
      item.asked_ms !== null && item.asked_ms !== undefined && now !== undefined
        ? ` ${coarseAge(now - item.asked_ms)} ago`
        : "";
    return `${name} · ${ticket} · blocked${asked}: ${cut(item.question, QUESTION_CHARS)}`;
  }
  if (kind === "stale_run") return `${name} · ${ticket} · heartbeat ${age(item.heartbeat_age_ms)}`;
  if (kind === "failed") {
    let text = `${name} · ${ticket} · failed`;
    if (item.ended_ms !== null && item.ended_ms !== undefined && now !== undefined) {
      text += ` ${coarseAge(now - item.ended_ms)} ago`;
    }
    if (item.reason) text += `: ${cut(item.reason, REASON_CHARS)}`;
    return text;
  }
  if (kind === "supervisor") {
    let text = `${name} · supervisor ${item.state ?? "none"}`;
    if (item.heartbeat_age_ms !== null && item.heartbeat_age_ms !== undefined) {
      text += ` · ${coarseAge(item.heartbeat_age_ms)}`;
    }
    return text;
  }
  return null;
}

/** The daemon's own `/attention` items, one row each in its order; the
 *  level the daemon's own word. */
function daemonRows(name: string, answer: Attention): { rows: Row[]; level: Level } {
  const rows: Row[] = [];
  for (const item of answer.items ?? []) {
    const label = itemRow(name, item, answer.now);
    if (label !== null) rows.push({ label, level: DAEMON_LEVEL[item.level ?? ""] ?? "attention" });
  }
  const fallback: Level = rows.length > 0 ? "attention" : "idle";
  return { rows, level: DAEMON_LEVEL[answer.level ?? ""] ?? fallback };
}

/** The rows computed from `/status` alone, for a daemon that does not
 *  answer `/attention`: a heartbeat past the daemon's own threshold and a
 *  supervisor that is `stale` or `none`. */
function localRows(name: string, status: Status): { rows: Row[]; level: Level } {
  const rows: Row[] = [];
  let level: Level = "idle";
  const staleMs = status.thresholds?.heartbeat_stale_ms;
  for (const run of status.runs ?? []) {
    const beat = run.heartbeat_age_ms;
    if (staleMs != null && beat != null && beat > staleMs) {
      rows.push({ label: `${name} · ${run.ticket} · heartbeat ${age(beat)}`, level: "attention" });
      level = worse(level, "attention");
    } else {
      level = worse(level, "working");
    }
  }
  const sup = status.supervisor ?? {};
  if (sup.state === "stale" || sup.state === "none") {
    let label = `${name} · supervisor ${sup.state}`;
    if (sup.heartbeat_age_ms != null) label += ` · ${coarseAge(sup.heartbeat_age_ms)}`;
    rows.push({ label, level: "attention" });
    level = worse(level, "attention");
  }
  return { rows, level };
}

/** Why an `/attention` answer cannot be rendered, or null when it can or
 *  when it is the 404 of a daemon older than the path. Anything else is a
 *  failure the operator must see: the items it hid are the ones this side
 *  cannot compute. */
function attentionError(answer: FetchResult<Attention> | undefined): string | null {
  if (answer === undefined) return null;
  if (answer.ok) return Array.isArray(answer.body?.items) ? null : "no items in answer";
  if (answer.kind === "http") return answer.status === 404 ? null : `HTTP ${answer.status}`;
  if (answer.kind === "unauthorized") return "HTTP 401";
  return answer.error || "unreachable";
}

/** The "needs you" rows for one reachable daemon and the level they carry. */
function attentionRows(
  name: string,
  status: Status,
  answer: FetchResult<Attention> | undefined,
): { rows: Row[]; level: Level } {
  if (answer?.ok && Array.isArray(answer.body?.items)) return daemonRows(name, answer.body);
  const local = localRows(name, status);
  const why = attentionError(answer);
  if (why === null) return local;
  const row: Row = { label: `${name} · /attention failed: ${cut(why, REASON_CHARS)}`, level: "bad" };
  return { rows: [row, ...local.rows], level: "bad" };
}

/** The newest `/runs` row whose outcome is `merged`, or null. */
function lastMerge(runs: Runs): { ticket: string; ended_ms?: number | null } | null {
  const rows = runs.rows ?? [];
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    if (rows[i]?.outcome === "merged") return rows[i] ?? null;
  }
  return null;
}

/** `idle · last merge KO-n · 12m` from the `/runs` answer, the age against
 *  the daemon's `now`; `idle · nothing merged yet` without a merged row;
 *  `idle · queue empty` when `/runs` gave no answer. */
function idleText(status: Status, runs: FetchResult<Runs> | undefined): string {
  if (runs === undefined || !runs.ok || !("rows" in runs.body)) return "idle · queue empty";
  const row = lastMerge(runs.body);
  if (row === null) return "idle · nothing merged yet";
  let text = `idle · last merge ${row.ticket}`;
  if (row.ended_ms != null && status.now != null) text += ` · ${coarseAge(status.now - row.ended_ms)}`;
  return text;
}

/** One reachable daemon's project line: `NAME · PHASE KO-n · hb AGE` per
 *  live run, or the idle text; a 503 (no store yet) shows the daemon's own
 *  `error` text. */
function projectLine(
  name: string,
  status: FetchResult<Status>,
  runs: FetchResult<Runs> | undefined,
): Row {
  if (!status.ok) {
    if (status.kind === "unauthorized") return { label: `${name} · needs token`, level: "attention" };
    if (status.kind === "unreachable") return { label: `${name} · unreachable`, level: "bad" };
    const body = status.body as Status | undefined;
    return { label: `${name} · ${body?.error ?? `HTTP ${status.status}`}`, level: "attention" };
  }
  const body = status.body;
  if (!Array.isArray(body.runs)) return { label: `${name} · ${body.error ?? "no answer"}`, level: "attention" };
  if (body.runs.length === 0) return { label: `${name} · ${idleText(body, runs)}`, level: "idle" };
  const parts = body.runs.map((run) => `${run.phase} ${run.ticket} · hb ${age(run.heartbeat_age_ms)}`);
  return { label: `${name} · ${parts.join(", ")}`, level: "working" };
}

/** `1 host · 3 daemons`: one daemon per address polled, the hosts the
 *  distinct `host` values they report (an unreachable daemon reports none). */
export function hostsLine(peers: string[], statuses: Record<string, FetchResult<Status>>): string {
  const hosts = new Set<string>();
  for (const address of peers) {
    const status = statuses[address];
    if (status?.ok && status.body.host) hosts.add(status.body.host);
  }
  const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;
  return `${plural(hosts.size, "host")} · ${plural(peers.length, "daemon")}`;
}

/**
 * The tray menu and its level over one poll. `peers` is every address
 * polled, the console's own first; `statuses` and `attentions` hold each
 * address's answers (an address with no `/attention` entry is a daemon
 * that was not asked, rendered by the local rule). `now` is this poll's
 * reference moment in epoch milliseconds, used only where a daemon's
 * answer carries no `now` of its own.
 */
export function buildSummary(
  peers: string[],
  statuses: Record<string, FetchResult<Status>>,
  attentions: Record<string, FetchResult<Attention>>,
  now: number,
  options: SummaryOptions = {},
): Summary {
  const needsYou: Row[] = [];
  const projects: Row[] = [];
  let level: Level = "idle";
  for (const address of peers) {
    const status = statuses[address] ?? { ok: false, kind: "unreachable", error: "not polled" };
    const name = projectName(address, status.ok ? status.body : null);
    const line = projectLine(name, status, options.runs?.[address]);
    projects.push(line);
    level = worse(level, line.level);
    if (!status.ok) continue;
    const attention = attentions[address];
    const withNow =
      attention?.ok && attention.body.now === undefined
        ? { ...attention, body: { ...attention.body, now } }
        : attention;
    const got = attentionRows(name, status.body, withNow);
    needsYou.push(...got.rows);
    level = worse(level, got.level);
  }
  const show = (): void => options.actions?.showConsole?.();
  const items: TrayMenuItem[] = [];
  if (needsYou.length === 0) items.push({ label: "Nothing needs you", enabled: false });
  else items.push(...needsYou.map((row) => ({ label: row.label, click: show })));
  items.push({ type: "separator" });
  items.push(...projects.map((row) => ({ label: row.label, click: show })));
  items.push({ type: "separator" });
  items.push({ label: hostsLine(peers, statuses), enabled: false });
  items.push({ type: "separator" });
  items.push(...menuTemplate(options.state ?? { openAtLogin: false }, options.actions));
  return { items, level };
}

/** The poll's answer, as `pollAll` returns it, folded into the summary in
 *  one step so the caller cannot forget a part of it (the idle line needs
 *  `/runs`; `pollAll` only fetches it for idle daemons). */
export type PollAnswerLike = {
  peers: string[];
  statuses: Record<string, FetchResult<Status>>;
  attentions: Record<string, FetchResult<Attention>>;
  runs: Record<string, FetchResult<Runs>>;
};

export function summarizeAnswer(
  answer: PollAnswerLike,
  now: number,
  options: Omit<SummaryOptions, "runs"> = {},
): Summary {
  return buildSummary(answer.peers, answer.statuses, answer.attentions, now, { ...options, runs: answer.runs });
}
