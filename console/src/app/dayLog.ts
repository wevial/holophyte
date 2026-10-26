import { useContext } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { prefsSchema, type DayLogPrefs, type LedgerEntry, type Prefs } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { fetchJson, REQUEST_TIMEOUT_MS } from "../lib/poll";
import { useToast } from "./components/Toast";
import { OriginContext, PollDepsContext } from "./scope";

export type Preset = DayLogPrefs["preset"];
export type DayLogFilter = DayLogPrefs;

const item = (id: string, label: string, highlight = false) => ({ id, label, highlight });

export const LOG_GROUPS = [
  { id: "outcomes", label: "Outcomes", items: [item("merged", "Merged", true), item("failed", "Failed", true), item("pr-opened", "PR opened")] },
  { id: "needs-you", label: "Needs you", items: [item("parked", "Parked", true), item("merge-requeued", "Merge requeued", true)] },
  {
    id: "operator",
    label: "Operator actions",
    items: [
      item("requeues", "Requeues", true),
      item("steers", "Steers", true),
      item("notes", "Notes", true),
      item("approvals", "Approvals", true),
      item("pause-abort-start", "Pause, abort, start", true),
      item("factory-amendments", "Factory amendments", true),
    ],
  },
  {
    id: "review",
    label: "Review rounds",
    items: [item("verdicts", "Verdicts"), item("adjudications", "Adjudications"), item("review-caps", "Review caps"), item("witness-pre-checks", "Witness pre-checks")],
  },
  {
    id: "babysit",
    label: "Babysit and CI",
    items: [item("babysit-passes", "Babysit passes"), item("fix-rounds", "Fix rounds"), item("evidence", "Evidence"), item("supervisor-wake-ups", "Supervisor wake-ups")],
  },
  { id: "system", label: "Supervisor and system", items: [item("loop-starts", "Loop starts"), item("main-merged", "Main merged in"), item("other", "Other")] },
];

const ALL_ITEMS = LOG_GROUPS.flatMap((group) => group.items);

export const HIGHLIGHTS: DayLogFilter = { preset: "highlights", items: Object.fromEntries(ALL_ITEMS.map((each) => [each.id, each.highlight])), projects: {} };
export const EVERYTHING: DayLogFilter = { preset: "everything", items: Object.fromEntries(ALL_ITEMS.map((each) => [each.id, true])), projects: {} };

/** The entry's first line, cut at a code fence. */
function summaryOf(text: string): string {
  return text.trim().split("```")[0]!.split("\n")[0]!.trim() || "Code block";
}

const OPERATOR_ITEMS: Record<string, string> = { requeue: "requeues", steer: "steers", operator_note: "notes", approve: "approvals" };

const OPERATOR_PHRASES: Record<string, string> = {
  requeue: "operator requeued",
  steer: "operator steered",
  operator_note: "operator note",
  approve: "operator approved",
  pause: "operator paused",
  resume: "operator resumed",
  abort: "operator aborted",
  hold: "operator held",
  release_hold: "operator released the hold",
  launch_loop: "operator started the loop",
  babysit: "operator woke the babysitter",
  decide: "operator decided",
};

const FIX_ROUND = /^(Automatic fix|Check fix|Capture fix|Babysit review|Rewrote)/;

interface Rule {
  group: string;
  item: string;
  test: (entry: LedgerEntry, line: string) => boolean;
  phrase: (entry: LedgerEntry, line: string) => string;
}

const words = (text: string) => text.replaceAll("_", " ");
const humanAction = (line: string) => /^human (\w+)/.exec(line)?.[1];

function failedPhrase(entry: LedgerEntry, line: string): string {
  if (entry.failure_kind) return `failed: ${words(entry.failure_kind)}`;
  return /^FAILED verify\b/.test(line) ? "failed: verify" : "failed";
}

function parkPhrase(entry: LedgerEntry): string {
  const criterion = /criterion (C\d+) cannot be met as written/.exec(entry.text)?.[1];
  if (criterion) return `needs you: ${criterion} contradiction`;
  if (entry.text.includes("a thread needs a human's answer")) return "needs you: a thread to answer";
  return "needs you";
}

function verdictPhrase(line: string): string {
  const [, round, verdict] = /^Round (\d+): (\w+)/.exec(line)!;
  return `review round ${round}: ${verdict!.toUpperCase() === "APPROVE" ? "approve" : "changes asked"}`;
}

function adjudicationPhrase(line: string): string {
  const found = /after (\d+) review rounds?: (\w+)/i.exec(line);
  return found ? `adjudicated after ${found[1]} rounds: ${found[2]!.toLowerCase()}` : "adjudicated";
}

function reviewCapPhrase(line: string): string {
  const found = /^Review cap (\d+) for (\d+) changed lines/.exec(line);
  return found ? `review cap ${found[1]} · ${found[2]} lines` : "review cap";
}

function witnessPhrase(line: string): string {
  const found = /turn (\d+) of (\d+)/.exec(line);
  return found ? `witness pre-check ${found[1]} of ${found[2]}` : "witness pre-check";
}

const CHECKS: Record<string, string> = { success: "green", failure: "failed" };

function babysitPhrase(line: string): string {
  const pass = /^Babysit pass (\d+)/.exec(line)?.[1] ?? "";
  const checks = /checks (\w+)/.exec(line)?.[1];
  const checked = checks ? `checks ${CHECKS[checks] ?? checks}` : "";
  const threads = Number(/: (\d+) unresolved thread/.exec(line)?.[1] ?? 0);
  const open = threads > 0 ? `${threads} open ${threads === 1 ? "thread" : "threads"}` : "";
  const state = [open, checked].filter(Boolean).join(", ");
  return state ? `babysit pass ${pass}: ${state}` : `babysit pass ${pass}`;
}

function fixPhrase(line: string): string {
  const count = /^(Automatic fix|Check fix|Capture fix) round (\d+) of (\d+)/.exec(line);
  if (count) return `${count[1]!.toLowerCase()} ${count[2]} of ${count[3]}`;
  return line.startsWith("Rewrote") ? "description rewritten" : "babysit review fix";
}

const note = (prefix: string) => (entry: LedgerEntry, line: string) => entry.kind === "note" && line.startsWith(prefix);
const operator = (action: string) => (entry: LedgerEntry, line: string) => entry.source === "operator" && line.startsWith(`human ${action}:`);

const RULES: Rule[] = [
  { group: "outcomes", item: "merged", test: (entry) => entry.kind === "merge", phrase: () => "merged" },
  { group: "outcomes", item: "failed", test: (entry) => entry.kind === "failure", phrase: failedPhrase },
  { group: "outcomes", item: "pr-opened", test: note("PR OPEN:"), phrase: () => "PR opened" },
  { group: "needs-you", item: "parked", test: (entry) => entry.kind === "park", phrase: parkPhrase },
  { group: "needs-you", item: "merge-requeued", test: note("MERGE REQUEUED"), phrase: () => "needs you: merge requeued" },
  ...Object.entries(OPERATOR_ITEMS).map(([action, id]): Rule => ({
    group: "operator", item: id, test: operator(action), phrase: () => OPERATOR_PHRASES[action]!,
  })),
  {
    group: "operator",
    item: "pause-abort-start",
    test: (entry, line) => entry.source === "operator" && humanAction(line) != null,
    phrase: (_entry, line) => OPERATOR_PHRASES[humanAction(line)!] ?? `operator ${words(humanAction(line)!)}`,
  },
  {
    group: "operator",
    item: "factory-amendments",
    test: (_entry, line) => /^factory (requeue|steer)\b/.test(line),
    phrase: (_entry, line) => (line.startsWith("factory requeue") ? "factory requeued" : "factory steered"),
  },
  { group: "review", item: "verdicts", test: (entry, line) => entry.kind === "round" && /^Round \d+: (APPROVE|REQUEST_CHANGES)/.test(line), phrase: (_entry, line) => verdictPhrase(line) },
  { group: "review", item: "adjudications", test: (entry) => entry.kind === "adjudication", phrase: (_entry, line) => adjudicationPhrase(line) },
  { group: "review", item: "review-caps", test: note("Review cap"), phrase: (_entry, line) => reviewCapPhrase(line) },
  { group: "review", item: "witness-pre-checks", test: note("Witness pre-check"), phrase: (_entry, line) => witnessPhrase(line) },
  { group: "babysit", item: "babysit-passes", test: (entry, line) => entry.kind === "round" && line.startsWith("Babysit pass"), phrase: (_entry, line) => babysitPhrase(line) },
  { group: "babysit", item: "fix-rounds", test: (entry, line) => entry.kind === "round" && FIX_ROUND.test(line), phrase: (_entry, line) => fixPhrase(line) },
  { group: "babysit", item: "evidence", test: note("Evidence on"), phrase: () => "evidence captured" },
  { group: "babysit", item: "supervisor-wake-ups", test: (entry, line) => entry.kind === "intervention" && line.startsWith("supervisor babysit:"), phrase: () => "babysitter woken" },
  {
    group: "system",
    item: "loop-starts",
    test: (entry, line) => entry.kind === "intervention" && /^supervisor (launch_loop|launch_backoff):/.test(line),
    phrase: () => "loop started by supervisor",
  },
  { group: "system", item: "main-merged", test: note("Merged main"), phrase: () => "main merged into branch" },
];

const OTHER: Rule = { group: "system", item: "other", test: () => true, phrase: (_entry, line) => line.split(":")[0]!.trim() || line };

function ruleOf(entry: LedgerEntry): { rule: Rule; line: string } {
  const line = summaryOf(entry.text);
  return { rule: RULES.find((rule) => rule.test(entry, line)) ?? OTHER, line };
}

export function categoryOf(entry: LedgerEntry): Pick<Rule, "group" | "item"> {
  const { rule } = ruleOf(entry);
  return { group: rule.group, item: rule.item };
}

export function phraseOf(entry: LedgerEntry): string {
  const { rule, line } = ruleOf(entry);
  return rule.phrase(entry, line);
}

/** The filter as the tab shows it: a preset's own item set, or the saved
 *  manual choices with an unnamed item at its Highlights value. */
export function itemsOf(filter: DayLogFilter): Record<string, boolean> {
  if (filter.preset === "highlights") return HIGHLIGHTS.items;
  if (filter.preset === "everything") return EVERYTHING.items;
  return Object.fromEntries(ALL_ITEMS.map((each) => [each.id, filter.items[each.id] ?? each.highlight]));
}

export function projectOn(filter: DayLogFilter, project: string): boolean {
  return filter.preset !== "manual" || (filter.projects[project] ?? true);
}

/** Whether `filter` keeps `entry`; an entry with no project passes the
 *  project checks. */
export function keeps(filter: DayLogFilter, entry: LedgerEntry): boolean {
  if (entry.project != null && !projectOn(filter, projectName(entry.project))) return false;
  return itemsOf(filter)[categoryOf(entry).item] === true;
}

const prefsKey = (origin: string) => ["prefs", origin] as const;

const writes = new Map<string, Promise<unknown>>();

async function putDayLog(fetch: (url: string, init?: RequestInit) => Promise<Response>, url: string, filter: DayLogFilter): Promise<string | null> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: "PUT",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ day_log: filter }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (failure) {
    return failure instanceof Error ? failure.message : String(failure);
  }
  if (response.ok) return null;
  const body = (await response.json().catch(() => null)) as { error?: unknown } | null;
  return typeof body?.error === "string" && body.error ? body.error : `${url} answered ${response.status}`;
}

/** The Today's log filter saved at the page's origin daemon's `/prefs`,
 *  Highlights when none is saved or the read fails, and its writer: the
 *  page takes each change at once and a refused write says so in a toast. */
export function useDayLogFilter(): { filter: DayLogFilter; setFilter: (next: DayLogFilter) => void } {
  const origin = useContext(OriginContext);
  const deps = useContext(PollDepsContext);
  const client = useQueryClient();
  const toast = useToast();
  const url = `${origin}/prefs`;
  const { data } = useQuery({
    queryKey: prefsKey(origin),
    queryFn: () => fetchJson(deps.fetch, url, prefsSchema, AbortSignal.timeout(REQUEST_TIMEOUT_MS)),
    staleTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });
  const setFilter = (next: DayLogFilter) => {
    void client.cancelQueries({ queryKey: prefsKey(origin) }).then(() =>
      client.setQueryData<Prefs>(prefsKey(origin), (previous) => ({ updated_ms: null, ...previous, day_log: next })));
    const write = (writes.get(origin) ?? Promise.resolve()).then(() => putDayLog(deps.fetch, url, next)).then((reason) => {
      if (reason != null) toast(`Today's log filter not saved: ${reason}`);
    });
    writes.set(origin, write);
  };
  return { filter: data?.day_log ?? HIGHLIGHTS, setFilter };
}

const PALETTE = ["violet", "teal", "green", "blue"] as const;

interface ColouredHost {
  project: string | null;
  status: { colour?: string } | null;
}

/** Each project's key colour, by its name: its `/status` colour unless
 *  `auto`, else the palette's colour at its index in `hosts`' project order. */
export function projectColours(hosts: ColouredHost[]): Map<string, string> {
  const colours = new Map<string, string>();
  for (const host of hosts) {
    if (host.project == null) continue;
    const name = projectName(host.project);
    if (colours.has(name)) continue;
    const chosen = host.status?.colour;
    const colour = chosen && chosen !== "auto" ? chosen : PALETTE[colours.size % PALETTE.length]!;
    colours.set(name, `var(--p-${colour})`);
  }
  return colours;
}
