import { formatAge, formatClock, formatSpan } from "./format";
import type { AttentionItem, Run, Status } from "./types";

/** The item kinds `/attention` sends today (holophyte/serve.py `attention()`),
 *  plus `unreachable`, which the console adds for a daemon that stopped
 *  answering (lib/hosts.ts `hostItems`). `pr_open` is a run parked on its
 *  pull request: nobody owes the factory an answer, the PR waits on a
 *  review or a merge, so it is its own kind and not a question. */
export type Kind = "blocked" | "pr_open" | "stale_run" | "failed" | "supervisor" | "unreachable";
export const KINDS: Kind[] = ["blocked", "pr_open", "stale_run", "failed", "supervisor", "unreachable"];

/** A chip: every kind, or one of them. */
export type KindFilter = "all" | Kind;

export const CHIP_LABELS: Record<KindFilter, string> = {
  all: "All",
  blocked: "Questions",
  pr_open: "PRs",
  stale_run: "Stale runs",
  failed: "Failed",
  supervisor: "Supervisor",
  unreachable: "Unreachable",
};

export const PILL_TEXT: Record<Kind, string> = {
  blocked: "question",
  pr_open: "PR",
  stale_run: "stale run",
  failed: "failed",
  supervisor: "supervisor",
  unreachable: "unreachable",
};

/** The `pr_open` row's one action: the PR's URL in a new tab, no daemon
 *  route behind it (components/AttentionRow.tsx). */
export const OPEN_PR = "Open PR";

const ACTIONS: Record<Kind, string[]> = {
  blocked: ["Answer", "Requeue"],
  pr_open: [OPEN_PR],
  stale_run: ["Kill run", "Requeue"],
  failed: ["Requeue", "Mark needs_spec"],
  supervisor: ["Restart supervisor"],
  unreachable: [],
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
  const counts: Counts = { all: items.length, blocked: 0, pr_open: 0, stale_run: 0, failed: 0, supervisor: 0, unreachable: 0 };
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
 *  carries an age. `at` is the clock every item is aged against, or a
 *  function answering each item's own age (a `describe` per host). */
export function oldest(
  items: AttentionItem[],
  at: number | ((item: AttentionItem) => { ageMs: number | null }),
): { ageMs: number; ticket: string | null } | null {
  let best: { ageMs: number; ticket: string | null } | null = null;
  for (const item of items) {
    const ageMs = typeof at === "number" ? ageOf(item, at) : at(item).ageMs;
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

/** What `plainReason` read off a `runs.outcomeReason`: the one-sentence
 *  summary a row shows, and the branch and whole sha the reason named
 *  (`branch task/x preserved at SHA`, `work kept on task/x at SHA`). */
export interface PlainReason {
  sentence: string;
  branch: string | null;
  sha: string | null;
}

/** The reason families `holophyte/loop.py` writes as `outcomeReason`
 *  (`RunFailure` messages), each as the prefix or pattern its writer
 *  uses, in the order tried. The sentence is presentation: the daemon
 *  stays a mirror of the store and the verbatim line stays in the run
 *  detail. A family may read a detail off its match (the verdict, the
 *  budget). */
const FAMILIES: { match: RegExp; sentence: (m: RegExpMatchArray) => string }[] = [
  // `_terminal_adjudication()`: "terminal adjudication: FAIL; branch B preserved at SHA"
  { match: /^terminal adjudication: (\S+?);/, sentence: (m) => `Review adjudicated ${m[1]}` },
  // `_review_rounds()`: "preserved commits on B conflict with a main that moved on ...; branch B preserved at SHA"
  { match: /^preserved commits on \S+ conflict with a main/, sentence: () => "Preserved branch conflicts with the moved main" },
  // `run_verify` callers: "verify failed before merge; branch B preserved at SHA", "verify failed: ..."
  { match: /^verify failed\b/, sentence: () => "Verify command failed" },
  // `_implement()`: "implementer exceeded the N min budget; work kept on B at SHA"
  { match: /^implementer exceeded the (\d+) min budget/, sentence: (m) => `Ran past its ${m[1]} min budget` },
  // `_implement()`: "implementer made no commits; ..." / "implementer made no new commits; preserved work kept on B at SHA"
  { match: /^implementer made no (?:new )?commits\b/, sentence: () => "Implementer exited without committing" },
];

const PRESERVED = /\b(?:branch (\S+) preserved at|(?:work )?kept on (\S+) at) ([0-9a-f]{7,40})\b/;

/** One plain sentence for a `runs.outcomeReason`, with the branch and sha
 *  the line named when it named them. A reason from no known family
 *  shows its first line up to the first semicolon, so an unknown shape
 *  hides nothing. */
export function plainReason(raw: string): PlainReason {
  const preserved = raw.match(PRESERVED);
  const branch = preserved ? (preserved[1] ?? preserved[2] ?? null) : null;
  const sha = preserved ? (preserved[3] ?? null) : null;
  for (const family of FAMILIES) {
    const m = raw.match(family.match);
    if (m) return { sentence: family.sentence(m), branch, sha };
  }
  const firstLine = raw.split("\n", 1)[0] ?? "";
  return { sentence: firstLine.split(";", 1)[0]!.trim(), branch, sha };
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
    case "pr_open": {
      const asked = num(item.asked_ms);
      return {
        ...base,
        body: str(item.reason) ?? "",
        meta: joinMeta(runLabel(item), asked == null ? null : `parked at ${formatClock(asked)}`),
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
      const plain = plainReason(str(item.reason) ?? "");
      return {
        ...base,
        body: plain.sentence,
        meta: joinMeta(
          runLabel(item),
          attempt == null ? null : `strike ${attempt} of ${thresholds.strikes}`,
          plain.branch != null && plain.sha != null ? `${plain.branch} @ ${plain.sha.slice(0, 7)}` : null,
        ),
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
    case "unreachable": {
      const seen = num(item.last_seen_ms);
      return {
        ...base,
        ticket: null,
        body: `${str(item.host) ?? str(item.daemon) ?? "Daemon"} is not answering`,
        meta: joinMeta(
          str(item.daemon),
          seen == null ? "never answered" : `last seen ${formatAge(now - seen)} ago`,
          str(item.error),
        ),
      };
    }
    default:
      return { ...base, body: str(item.question) ?? str(item.reason) ?? "" };
  }
}
