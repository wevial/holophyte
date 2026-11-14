import type { SearchFinding, SearchRun, SearchScout, SearchTicket } from "../lib/contract.gen";
import { SCOUT_DOTS } from "./scout";
import { scoutState } from "./scouts";
import { STATES, stateOf, type State } from "./state";

/** Where a palette entry goes when chosen. */
type PaletteGo =
  | { kind: "page"; to: PagePath; q?: string }
  | { kind: "ticket"; project: string; key: string }
  | { kind: "run"; project: string; run: number }
  | { kind: "scout"; project: string; scout: number }
  | { kind: "project"; name: string | null }
  | { kind: "action"; action: PaletteAction; query?: string };

export type PaletteAction = "new-ticket" | "launch-scout" | "switch-theme" | "run-sweep";

/** A glyph is a state's dot or a kind's icon, each with its accessible name. */
export type PaletteGlyph = { state: State; label: string } | { icon: GlyphIcon; label: string; tone?: string };

export type GlyphIcon = "page" | "project" | "all-projects" | "finding" | "action";

export interface PaletteEntry {
  key: string;
  title: string;
  meta: string;
  glyph: PaletteGlyph;
  go: PaletteGo;
  /** Why the entry cannot be chosen, when it cannot. */
  disabled?: string;
}

export interface PaletteGroup {
  name: string;
  entries: PaletteEntry[];
}

export const PAGES = [
  { to: "/", label: "Overview" },
  { to: "/runs", label: "Runs" },
  { to: "/tickets", label: "Tickets" },
  { to: "/findings", label: "Findings" },
  { to: "/scouts", label: "Scouts" },
  { to: "/flow", label: "Flow" },
  { to: "/settings/appearance", label: "Appearance" },
] as const;

type PagePath = (typeof PAGES)[number]["to"];

export type Chip = ">" | "#" | "@";

export const CHIPS: Record<Chip, string> = { ">": "Actions", "#": "Tickets", "@": "Runs" };

/** A leading scope character of `text` becomes a chip when none is set. */
export function takeChip(chip: Chip | null, text: string): { chip: Chip | null; text: string } {
  const lead = text.charAt(0);
  if (chip == null && Object.hasOwn(CHIPS, lead)) return { chip: lead as Chip, text: text.slice(1) };
  return { chip, text };
}

const TICKET_KEY = /^[A-Za-z][A-Za-z0-9]*-\d+$/;

export const isTicketKey = (text: string) => TICKET_KEY.test(text);

/** `text` split around each case-blind occurrence of `query`, the matches flagged. */
export function marks(text: string, query: string): { text: string; match: boolean }[] {
  const needle = query.toLowerCase();
  if (needle === "") return [{ text, match: false }];
  const spans: { text: string; match: boolean }[] = [];
  const lower = text.toLowerCase();
  let from = 0;
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, from)) {
    if (at > from) spans.push({ text: text.slice(from, at), match: false });
    spans.push({ text: text.slice(at, at + needle.length), match: true });
    from = at + needle.length;
  }
  if (from < text.length) spans.push({ text: text.slice(from), match: false });
  return spans;
}

const RECENT_KEY = "holophyte.palette.recent";
const RECENT_MAX = 5;

let kept: PaletteEntry[] = [];

function isEntry(value: unknown): value is PaletteEntry {
  const entry = value as PaletteEntry;
  return typeof entry === "object" && entry != null && typeof entry.key === "string" && typeof entry.title === "string"
    && typeof entry.meta === "string" && typeof entry.glyph === "object" && typeof entry.go === "object" && entry.go?.kind !== "action";
}

/** The entries last opened from the palette, newest first: storage's, or
 *  this page's own while storage is blocked. */
export function readRecent(): PaletteEntry[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(stored) ? stored.filter(isEntry).slice(0, RECENT_MAX) : [];
  } catch {
    return kept;
  }
}

export function pushRecent(entry: PaletteEntry): void {
  const { disabled: _disabled, ...stored } = entry;
  kept = [stored, ...readRecent().filter((each) => each.key !== entry.key)].slice(0, RECENT_MAX);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(kept));
  } catch {
    // Storage may be disabled; the list still holds for this page.
  }
}

const TICKET_STATES: Record<string, State> = {
  merged: "merged",
  abandoned: "abandoned",
  needs_spec: "todo",
  blocked_on_deps: "todo",
  ready: "queued",
  in_flight: "running",
  blocked_on_operator: "needs_you",
};

const words = (status: string) => status.replaceAll("_", " ");

export function ticketEntry(project: string, hit: SearchTicket, base: string): PaletteEntry {
  return {
    key: `ticket:${base}:${hit.ticket}`,
    title: `${hit.ticket} ${hit.title}`,
    meta: `${project} · ${words(hit.status)}`,
    glyph: { state: TICKET_STATES[hit.status] ?? "backlog", label: words(hit.status) },
    go: { kind: "ticket", project, key: hit.ticket },
  };
}

export function runEntry(project: string, hit: SearchRun, base: string): PaletteEntry {
  const state = stateOf({ run: hit, staleMs: Number.POSITIVE_INFINITY });
  return {
    key: `run:${base}:${hit.id}`,
    title: `run ${hit.id} · ${hit.ticket} ${hit.title}`,
    meta: `${project} · ${words(hit.outcome ?? hit.phase)}`,
    glyph: { state, label: STATES[state].word },
    go: { kind: "run", project, run: hit.id },
  };
}

export function scoutEntry(project: string, hit: SearchScout, base: string): PaletteEntry {
  const state = scoutState(hit.phase);
  return {
    key: `scout:${base}:${hit.id}`,
    title: hit.title,
    meta: `${project} · scout ${hit.id} · ${hit.phase}`,
    glyph: { state: SCOUT_DOTS[state], label: state },
    go: { kind: "scout", project, scout: hit.id },
  };
}

export function findingEntry(project: string, hit: SearchFinding, index: number, base: string): PaletteEntry {
  const severity = hit.severity.toUpperCase();
  return {
    key: `finding:${base}:${hit.run}:${hit.round}:${index}`,
    title: `${severity} · ${hit.message}`,
    meta: `${hit.path}${hit.line == null ? "" : `:${hit.line}`} · run ${hit.run} round ${hit.round}`,
    glyph: { icon: "finding", label: `${severity} finding`, tone: severity === "P1" ? "text-bad" : "text-need" },
    go: { kind: "run", project, run: hit.run },
  };
}

export function pageEntry(page: (typeof PAGES)[number], q?: string): PaletteEntry {
  return {
    key: q == null ? `page:${page.to}` : `page:${page.to}?q`,
    title: q == null ? page.label : `${page.label} matching "${q}"`,
    meta: q == null ? "page" : `${page.label.toLowerCase()} page · search`,
    glyph: { icon: "page", label: "page" },
    go: q == null ? { kind: "page", to: page.to } : { kind: "page", to: page.to, q },
  };
}

export function projectEntry(name: string | null, current: boolean): PaletteEntry {
  return {
    key: `project:${name ?? ""}`,
    title: name ?? "All projects",
    meta: current ? "project · current scope" : "project",
    glyph: { icon: name == null ? "all-projects" : "project", label: "project" },
    go: { kind: "project", name },
  };
}
