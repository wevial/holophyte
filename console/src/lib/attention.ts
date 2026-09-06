import { formatAge, formatClock, formatSpan } from "./format";
import type { AttentionItem, Run, Status } from "./types";

/** The item kinds `/attention` sends today (holophyte/serve.py `attention()`). */
export type Kind = "blocked" | "stale_run" | "failed" | "supervisor";
export const KINDS: Kind[] = ["blocked", "stale_run", "failed", "supervisor"];

/** A chip: every kind, or one of them. */
export type KindFilter = "all" | Kind;

export const CHIP_LABELS: Record<KindFilter, string> = {
  all: "All",
  blocked: "Questions",
  stale_run: "Stale runs",
  failed: "Failed",
  supervisor: "Supervisor",
};

export const PILL_TEXT: Record<Kind, string> = {
  blocked: "question",
  stale_run: "stale run",
  failed: "failed",
  supervisor: "supervisor",
};

const ACTIONS: Record<Kind, string[]> = {
  blocked: ["Answer", "Requeue"],
  stale_run: ["Kill run", "Requeue"],
  failed: ["Requeue", "Mark needs_spec"],
  supervisor: ["Restart supervisor"],
};

/** `"all"`, or a project path as the rail selects it. An item belongs to
 *  the project its `project` field names; the band stamps the serving
 *  daemon's project on items that carry none before filtering. */
export type ProjectChoice = "all" | string;

export function filterItems(items: AttentionItem[], kind: KindFilter, project: ProjectChoice): AttentionItem[] {
  return items.filter(
    (item) => (kind === "all" || item.kind === kind) && (project === "all" || item.project === project),
  );
}

export type Counts = Record<KindFilter, number>;

export function countsByKind(items: AttentionItem[]): Counts {
  const counts: Counts = { all: items.length, blocked: 0, stale_run: 0, failed: 0, supervisor: 0 };
  for (const item of items) {
    if ((KINDS as string[]).includes(item.kind)) counts[item.kind as Kind] += 1;
  }
  return counts;
}

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);
const str = (value: unknown): string | null => (typeof value === "string" ? value : null);

/** How long an item has waited: `asked_ms`/`ended_ms` against `now`, or
 *  the heartbeat age the item carries; null when it says none of them. */
export function ageOf(item: AttentionItem, now: number): number | null {
  const asked = num(item.asked_ms);
  if (asked != null) return now - asked;
  const ended = num(item.ended_ms);
  if (ended != null) return now - ended;
  return num(item.heartbeat_age_ms);
}

/** The item that has waited longest, with its age; null when no item
 *  carries an age. */
export function oldest(items: AttentionItem[], now: number): { ageMs: number; ticket: string | null } | null {
  let best: { ageMs: number; ticket: string | null } | null = null;
  for (const item of items) {
    const ageMs = ageOf(item, now);
    if (ageMs != null && (best == null || ageMs > best.ageMs)) best = { ageMs, ticket: str(item.ticket) };
  }
  return best;
}

export interface Description {
  pill: string;
  ticket: string | null;
  body: string;
  meta: string | null;
  ageMs: number | null;
  actions: string[];
}

export interface DescribeContext {
  /** `/status.now`; defaults to the wall clock. */
  now?: number;
  /** `/status.runs`, joined on `run` for the time-box clause. */
  runs?: Run[];
}

function runLabel(item: AttentionItem): string | null {
  const run = num(item.run);
  return run == null ? null : `run #${run}`;
}

function joinMeta(...parts: (string | null)[]): string | null {
  const kept = parts.filter((part): part is string => part != null);
  return kept.length > 0 ? kept.join(" · ") : null;
}

function overTimeBox(item: AttentionItem, runs: Run[] | undefined): string {
  const id = num(item.run);
  const run = id == null ? undefined : runs?.find((candidate) => candidate.id === id);
  if (!run || !(run.elapsed_ms > run.time_box_ms)) return "";
  return ` and ${formatSpan(run.elapsed_ms - run.time_box_ms)} over its ${formatAge(run.time_box_ms)} time box`;
}

/** One row's text from an `/attention` item. Fields a newer daemon adds
 *  (`asked_ms`, `run` on blocked, `attempt` on failed) show only when present. */
export function describe(
  item: AttentionItem,
  thresholds: Status["thresholds"],
  context: DescribeContext = {},
): Description {
  const now = context.now ?? Date.now();
  const kind = item.kind as Kind;
  const base: Description = {
    pill: PILL_TEXT[kind] ?? item.kind.replace(/_/g, " "),
    ticket: str(item.ticket),
    body: "",
    meta: null,
    ageMs: ageOf(item, now),
    actions: ACTIONS[kind] ?? [],
  };
  switch (kind) {
    case "blocked": {
      const asked = num(item.asked_ms);
      return {
        ...base,
        body: str(item.question) ?? "",
        meta: joinMeta(runLabel(item), asked == null ? null : `asked at ${formatClock(asked)}`),
      };
    }
    case "stale_run": {
      const age = num(item.heartbeat_age_ms) ?? 0;
      const phase = str(item.phase) ?? "unknown";
      return {
        ...base,
        body: `No heartbeat for ${formatSpan(age)} while ${phase}${overTimeBox(item, context.runs)}`,
        meta: joinMeta(runLabel(item), str(item.phase)),
      };
    }
    case "failed": {
      const attempt = num(item.attempt);
      return {
        ...base,
        body: str(item.reason) ?? "",
        meta: joinMeta(runLabel(item), attempt == null ? null : `strike ${attempt} of ${thresholds.strikes}`),
      };
    }
    case "supervisor": {
      const age = num(item.heartbeat_age_ms);
      return {
        ...base,
        body:
          age == null
            ? `Supervisor is ${str(item.state) ?? "not live"}`
            : `Supervisor heartbeat is ${formatSpan(age)} old (threshold ${formatAge(thresholds.heartbeat_stale_ms)})`,
      };
    }
    default:
      return { ...base, body: str(item.question) ?? str(item.reason) ?? "" };
  }
}
