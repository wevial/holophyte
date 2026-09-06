import type { ShippedRow } from "./types";

const DAY_MS = 86_400_000;

/** One local calendar day of the ledger, newest first, as `groupByDay`
 *  cuts it: `key` is the `YYYY-MM-DD` of the day, `daysAgo` how many
 *  calendar days before `now`'s day it falls (0 today, 1 yesterday). */
export interface DayGroup {
  key: string;
  label: string;
  daysAgo: number;
  rows: ShippedRow[];
}

/** `YYYY-MM-DD` of `ms` in `tz` (the browser's zone when omitted). */
function dayKey(ms: number, tz?: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(ms));
  const part = (type: string) => parts.find((candidate) => candidate.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** The key's day as a UTC instant, for counting calendar days between keys. */
function keyInstant(key: string): number {
  const [year, month, day] = key.split("-").map(Number) as [number, number, number];
  return Date.UTC(year, month - 1, day);
}

/** "Fri Sep 5" for `ms` in `tz`: the short weekday, month and day, joined
 *  by spaces rather than the locale's comma. */
function dayName(ms: number, tz?: string): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    weekday: "short",
    month: "short",
    day: "numeric",
  }).formatToParts(new Date(ms));
  const part = (type: string) => parts.find((candidate) => candidate.type === type)?.value ?? "";
  return `${part("weekday")} ${part("month")} ${part("day")}`;
}

/** The heading for a day `daysAgo` days before `now`'s: "Today · Fri Sep 5",
 *  "Yesterday · Thu Sep 4", then the bare "Wed Sep 3". */
export function dayLabel(ms: number, daysAgo: number, tz?: string): string {
  const name = dayName(ms, tz);
  if (daysAgo === 0) return `Today · ${name}`;
  if (daysAgo === 1) return `Yesterday · ${name}`;
  return name;
}

/**
 * Rows under their local calendar day of `ended_ms`, one group per day in
 * order of first appearance (newest first when the rows are), each keeping
 * its rows' order. `now` is the daemon's clock, so "Today" is its day, not
 * the browser's; `tz` pins the zone for tests and defaults to the browser's.
 */
export function groupByDay(rows: ShippedRow[], now: number, tz?: string): DayGroup[] {
  const today = keyInstant(dayKey(now, tz));
  const groups: DayGroup[] = [];
  for (const row of rows) {
    const key = dayKey(row.ended_ms, tz);
    const existing = groups.find((group) => group.key === key);
    if (existing) {
      existing.rows.push(row);
      continue;
    }
    const daysAgo = Math.round((today - keyInstant(key)) / DAY_MS);
    groups.push({ key, label: dayLabel(row.ended_ms, daysAgo, tz), daysAgo, rows: [row] });
  }
  return groups;
}

/** The last `days` calendar days of `groups`, today counting as the first;
 *  `days` of 1 is today only. */
export function withinDays(groups: DayGroup[], days: number): DayGroup[] {
  return groups.filter((group) => group.daysAgo < days);
}

export type BoxClass = "ok" | "warn" | "over";

/** Actual over the box: ok at or under 80 %, warn above 80 % up to 100 %,
 *  over past 100 %; null with no box to compare against. */
export function boxRatio(actualMin: number, estimateMin: number | null): BoxClass | null {
  if (estimateMin == null || estimateMin <= 0) return null;
  const ratio = actualMin / estimateMin;
  if (ratio > 1) return "over";
  if (ratio > 0.8) return "warn";
  return "ok";
}

/** The bar's fill as a share of its track, capped at full; empty with no box. */
export function boxFill(actualMin: number, estimateMin: number | null): number {
  if (estimateMin == null || estimateMin <= 0) return 0;
  return Math.min(1, Math.max(0, actualMin / estimateMin));
}

/** "18m / 30m" beside the bar; "18m / —" with no box. */
export function minutesLabel(actualMin: number, estimateMin: number | null): string {
  const box = estimateMin == null || estimateMin <= 0 ? "—" : `${Math.round(estimateMin)}m`;
  return `${Math.round(actualMin)}m / ${box}`;
}

/** The signed difference of the rounded minutes: "−12m", "+8m", "0m";
 *  empty with no box. */
export function deltaLabel(actualMin: number, estimateMin: number | null): string {
  if (estimateMin == null || estimateMin <= 0) return "";
  const delta = Math.round(actualMin) - Math.round(estimateMin);
  if (delta < 0) return `−${-delta}m`;
  if (delta > 0) return `+${delta}m`;
  return "0m";
}

export type DeltaTone = "ok" | "over" | "muted";

/** Under the box reads in the ok text, past it in the over text, on it muted. */
export function deltaTone(actualMin: number, estimateMin: number | null): DeltaTone {
  if (estimateMin == null || estimateMin <= 0) return "muted";
  const delta = Math.round(actualMin) - Math.round(estimateMin);
  return delta < 0 ? "ok" : delta > 0 ? "over" : "muted";
}

/** The median of the rows' review rounds; null with no rows. An even
 *  count averages the middle two. */
export function medianRounds(rows: { rounds: number }[]): number | null {
  if (rows.length === 0) return null;
  const sorted = rows.map((row) => row.rounds).sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2;
}

/** `existing` and `incoming` as one ledger: one row per id, the newer
 *  answer winning, newest end first and ties by id. */
export function mergeRows(existing: ShippedRow[], incoming: ShippedRow[]): ShippedRow[] {
  const byId = new Map<number, ShippedRow>();
  for (const row of existing) byId.set(row.id, row);
  for (const row of incoming) byId.set(row.id, row);
  return [...byId.values()].sort((a, b) => b.ended_ms - a.ended_ms || b.id - a.id);
}

/** The short form of a merge sha; empty with none. */
export function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 7) : "";
}
