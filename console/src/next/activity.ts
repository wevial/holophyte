import type { ConsoleAsk, Round, RoundCriterion, RunDetail, RunEvent, SteerNote } from "../lib/contract.gen";
import { severityCounts } from "../lib/findings";
import { edge, isFinalCheck, orderEvents, summarize } from "../lib/log";
import { prLabel, shortSha } from "../lib/shipped";
import { noteAuthor } from "./progress";
import { CHECK_FIX_ROUND } from "./run";

export type ActivityKind = "before" | "implement" | "round" | "final" | "pull_request" | "pickup" | "threads" | "checks" | "fix_ci";

export type ActivityTone = "ok" | "warn" | "bad" | "wait" | "act" | "live" | "neutral";

export interface ActivityStep {
  at: number;
  text: string;
  ms: number | null;
  quiet: boolean;
}

export interface ActivityGroup {
  kind: ActivityKind;
  at: number;
  title: string;
  /** Short phrases the timeline joins with " · ". */
  sub: string[];
  /** Null while running, and for the notes carried in before the run. */
  ms: number | null;
  tone: ActivityTone;
  running: boolean;
  steps: ActivityStep[];
  round: Round | null;
  /** The round's criteria that were not met or unwitnessed. */
  marks: RoundCriterion[];
}

export interface ActivityNow {
  what: string;
  since: number;
  detail: string | null;
}

export interface RunActivity {
  groups: ActivityGroup[];
  now: ActivityNow | null;
}

interface Draft extends Omit<ActivityGroup, "ms" | "running"> {
  end: number | null;
  what?: string;
}

const NEVER_STEPS = new Set(["turn_progress", "pr_seen_commits", "pr_empty_wakes", "pr_text_sha", "phase_change"]);
export const PASSED = new Set(["pass", "approved"]);

const WRITING = /^writing the pull request text for /;
const PUSHING = /^pushing \S+ to \S+ and opening its pull request/;
const OPENED = /^(?:pull request open|adopted the branch's open pull request): (\S+)/;
const RESUMING = /^resuming run (\d+)'s candidate \S+ at ([0-9a-f]+) on (\S+)/;
const RERUN = /^rerunning the failed jobs of (.+?) on \S+:/;
const LINE_DELTA = /line delta ([+-]\d+)/;
const CARRIED_AT = /\bat ([0-9a-f]{7,})\b/;
const WITNESSED = /(\d+) of (\d+) criteria witnessed/;

const plural = (count: number, noun: string) => `${count} ${noun}${count === 1 ? "" : "s"}`;

const isEdge = (event: RunEvent, from: string | null, to: string | null) => {
  const walked = event.kind === "phase_change" ? edge(event.summary) : null;
  return walked != null && (from == null || walked[0] === from) && (to == null || walked[1] === to);
};

function draft(kind: ActivityKind, at: number, title: string, fields: Partial<Draft> = {}): Draft {
  return { kind, at, title, sub: [], tone: "ok", steps: [], round: null, marks: [], end: null, ...fields };
}

/** Notes this run read, and unread ones made before it ended. */
function runNotes(detail: RunDetail): SteerNote[] {
  const { id, outcome, ended_ms: ended } = detail.run;
  return detail.steer_notes
    .filter((note) => !note.withdrawn || note.consumed_ms != null)
    .filter((note) => note.consumed_by === id || (note.consumed_by == null && (outcome == null || ended == null || note.at <= ended)));
}

const noteStep = (note: SteerNote): ActivityStep => ({
  at: note.at,
  text: `${noteAuthor(note.author, "operator")} ${note.kind === "hint" ? "hinted" : "steered"}: ${note.note}`,
  ms: null,
  quiet: false,
});

function beforeGroup(carried: SteerNote[]): Draft[] {
  if (carried.length === 0) return [];
  const amendments = carried.filter((note) => note.kind === "amendment").length;
  const hints = carried.length - amendments;
  const sub = [amendments > 0 && `${plural(amendments, "amendment")} carried in`, hints > 0 && `${plural(hints, "hint")} carried in`];
  return [
    draft("before", Math.min(...carried.map((note) => note.at)), "Before this run", {
      sub: sub.filter((phrase) => phrase !== false),
      tone: "neutral",
      steps: carried.map(noteStep),
    }),
  ];
}

function implementGroup(detail: RunDetail, events: RunEvent[], used: Set<RunEvent>): Draft[] {
  const resumed = events.some((event) => event.kind === "pull_request" && RESUMING.test(event.summary));
  if (resumed && !events.some((event) => isEdge(event, null, "working"))) return [];
  const start = detail.run.started_ms;
  const end = events.find((event) => isEdge(event, "working", "verifying"))?.at ?? null;
  const inside = events.filter((event) => event.at >= start && (end == null || event.at < end));
  const sub: string[] = [];
  for (const event of inside) {
    const delta = event.kind === "trim" ? LINE_DELTA.exec(event.summary) : null;
    const carried = event.kind === "carried_candidate" ? CARRIED_AT.exec(event.summary) : null;
    if (delta) sub.push(`trimmed ${Number(delta[1]) < 0 ? "−" : "+"}${Math.abs(Number(delta[1]))} lines`);
    if (carried) sub.push(`carried candidate at ${shortSha(carried[1]!)}`);
    if (delta || carried) used.add(event);
  }
  return [draft("implement", start, "Implement", { sub, end })];
}

/** Each round recorded once babysitting began, with the babysit step it was recorded under. */
function babysitRounds(detail: RunDetail, events: RunEvent[]): Map<Round, RunEvent> {
  const steps = events.filter((event) => event.kind === "babysit_step");
  const held = new Map<Round, RunEvent>();
  for (const round of detail.rounds) {
    const step = steps.findLast((step) => step.at <= round.started_ms);
    if (step) held.set(round, step);
  }
  return held;
}

function verifyText(events: RunEvent[], from: number, to: number, used: Set<RunEvent>): string {
  const precheck = events.filter((event) => event.kind === "witness_precheck" && event.at >= from && event.at <= to).at(-1);
  if (precheck == null) return "Verify";
  used.add(precheck);
  const [head = "", ...gaps] = precheck.summary.split("\n");
  const count = WITNESSED.exec(head);
  if (count == null) return `Verify · ${head}`;
  const named = [...new Set(gaps.flatMap((line) => line.match(/\bC\d+\b/g) ?? []))];
  return [`Verify`, `${count[1]} of ${count[2]} witnessed`, ...(named.length > 0 ? [named.join(", ")] : [])].join(" · ");
}

function reviewWords(round: Round): Pick<Draft, "sub" | "tone"> {
  if (round.ended_ms == null) return { sub: [], tone: "neutral" };
  if (PASSED.has(round.verdict)) return { sub: ["passed"], tone: "ok" };
  const counts = severityCounts(round.findings);
  const sub = [
    ...(counts.must > 0 ? [`${counts.must} must-fix`] : []),
    ...(counts.should > 0 ? [`${counts.should} should-fix`] : []),
    ...(counts.nit > 0 ? [plural(counts.nit, "nit")] : []),
  ];
  if (round.verdict !== "changes_requested") return { sub: [round.verdict.replaceAll("_", " "), ...sub], tone: "bad" };
  return { sub: sub.length > 0 ? sub : ["changes requested"], tone: counts.must > 0 ? "bad" : "warn" };
}

function finalWords(round: Round): Pick<Draft, "sub" | "tone"> {
  if (round.ended_ms == null) return { sub: [], tone: "neutral" };
  const count = (status: RoundCriterion["status"]) => round.criteria.filter((criterion) => criterion.status === status).length;
  if (PASSED.has(round.verdict)) return { sub: [`adjudication passed, ${count("met")} of ${round.criteria.length} met`], tone: "ok" };
  const notMet = count("not_met");
  const unwitnessed = count("unwitnessed");
  const sub = ["adjudication FAIL", ...(notMet > 0 ? [`${notMet} not met`] : []), ...(unwitnessed > 0 ? [`${unwitnessed} unwitnessed`] : [])];
  return { sub, tone: "bad" };
}

const span = (from: number, to: number | null | undefined) => (to == null ? null : to - from);

function roundGroup(detail: RunDetail, events: RunEvent[], round: Round, opened: number, nextStart: number, used: Set<RunEvent>): Draft {
  const final = round.round > detail.run.max_rounds || isFinalCheck(round, events);
  const verify: ActivityStep = { at: opened, text: verifyText(events, opened, round.started_ms, used), ms: round.started_ms - opened, quiet: false };
  const model = round.reviewer_model ? ` · ${round.reviewer_model}` : "";
  const review: ActivityStep = { at: round.started_ms, text: `${final ? "Adjudication" : "Review"}${model}`, ms: span(round.started_ms, round.ended_ms), quiet: false };
  const marks = round.criteria.filter((criterion) => criterion.status !== "met");
  if (final) return draft("final", opened, "Final check", { ...finalWords(round), steps: [verify, review], round, marks, end: round.ended_ms });
  const loop = events.find((event) => isEdge(event, "reviewing", "addressing") && event.at >= round.started_ms && event.at < nextStart);
  const fixed = loop == null ? undefined : events.find((event) => isEdge(event, "addressing", "verifying") && event.at >= loop.at);
  const fix: ActivityStep[] = loop == null ? [] : [{ at: loop.at, text: "Fix", ms: span(loop.at, fixed?.at), quiet: false }];
  const end = loop == null ? round.ended_ms : (fixed?.at ?? null);
  return draft("round", opened, `Round ${round.round}`, { ...reviewWords(round), steps: [verify, review, ...fix], round, marks, end });
}

/** Each review round from the verify that led into it, then the verify a live run is in before its next round. */
function roundGroups(detail: RunDetail, events: RunEvent[], babysat: Map<Round, RunEvent>, used: Set<RunEvent>): Draft[] {
  const verifies = events.filter((event) => isEdge(event, null, "verifying") && !isEdge(event, "verifying", null));
  const reviewed = detail.rounds.filter((round) => !babysat.has(round)).sort((a, b) => a.round - b.round);
  let claimed = -1;
  const groups = reviewed.map((round, index) => {
    const opener = verifies.findLastIndex((event, at) => at > claimed && event.at <= round.started_ms);
    if (opener >= 0) claimed = opener;
    const opened = opener >= 0 ? verifies[opener]!.at : round.started_ms;
    return roundGroup(detail, events, round, opened, reviewed[index + 1]?.started_ms ?? Infinity, used);
  });
  const pending = verifies[claimed + 1];
  const handedOver = events.find((event) => event.kind === "pull_request" || event.kind === "babysit_step");
  if (detail.run.outcome != null || pending == null || (handedOver != null && handedOver.at <= pending.at)) return groups;
  const number = (reviewed.at(-1)?.round ?? 0) + 1;
  const final = number > detail.run.max_rounds;
  const verify: ActivityStep = { at: pending.at, text: verifyText(events, pending.at, Infinity, used), ms: null, quiet: false };
  return [...groups, draft(final ? "final" : "round", pending.at, final ? "Final check" : `Round ${number}`, { steps: [verify], tone: "neutral" })];
}

/** The pull request's writing, push and opening as one group, and a resumed run's pickup. */
function pullRequestGroups(events: RunEvent[], used: Set<RunEvent>): Draft[] {
  const groups: Draft[] = [];
  let open: { group: Draft; words: string[]; done: boolean } | null = null;
  for (const event of events.filter((event) => event.kind === "pull_request")) {
    const resumed = RESUMING.exec(event.summary);
    if (resumed) {
      used.add(event);
      const sub = [`run ${resumed[1]}'s candidate at ${shortSha(resumed[2]!)}`];
      groups.push(draft("pickup", event.at, `Picked up ${prLabel(resumed[3]!)}`, { sub, tone: "neutral" }));
      continue;
    }
    const opened = OPENED.exec(event.summary);
    const word = WRITING.test(event.summary) ? "PR text written from the diff" : PUSHING.test(event.summary) ? "pushed" : opened ? "opened" : null;
    if (word == null) continue;
    used.add(event);
    if (open == null || open.done) {
      open = { group: draft("pull_request", event.at, "Pull request"), words: [], done: false };
      groups.push(open.group);
    }
    open.words.push(word);
    if (opened) {
      open.group.title = prLabel(opened[1]!).replace(/^PR\b/, "Pull request");
      open.done = true;
    }
    open.group.steps = [{ at: open.group.at, text: open.words.join(" · "), ms: open.done ? event.at - open.group.at : null, quiet: false }];
  }
  return groups;
}

/** The check fix round a babysit `fix` step works on: the newest one since the step before it. */
function fixingRound(events: RunEvent[], fix: number): RegExpExecArray | null {
  const since = events.findLastIndex((event, at) => at < fix && event.kind === "babysit_step" && event.summary !== "fix");
  const round = events.findLastIndex((event, at) => at < fix && at > since && event.kind === "check_fix_round");
  return round < 0 ? null : CHECK_FIX_ROUND.exec(events[round]!.summary);
}

function babysitGroups(events: RunEvent[], babysat: Map<Round, RunEvent>, used: Set<RunEvent>): Draft[] {
  return events.flatMap((event, index): Draft[] => {
    if (event.kind !== "babysit_step") return [];
    const round = [...babysat].find(([, step]) => step === event)?.[0] ?? null;
    const held = { round, marks: round?.criteria.filter((criterion) => criterion.status !== "met") ?? [] };
    if (event.summary === "checks") {
      used.add(event);
      return [draft("checks", event.at, "Checks", held)];
    }
    if (event.summary === "threads") {
      used.add(event);
      const found = round?.findings.length ?? 0;
      return [draft("threads", event.at, "Review threads", { ...held, sub: round ? [plural(found, "thread")] : [], tone: found > 0 ? "warn" : "ok" })];
    }
    const fixing = event.summary === "fix" ? fixingRound(events, index) : null;
    if (fixing == null) return [];
    used.add(event);
    return [draft("fix_ci", event.at, "Fix CI", { ...held, sub: [`round ${fixing[1]} of ${fixing[2]}`], tone: "act", what: `Fixing CI · ${fixing[3]}` })];
  });
}

function checkText(event: RunEvent, events: RunEvent[]): string | null {
  if (event.kind === "check_rerun") {
    const rerun = RERUN.exec(event.summary);
    return rerun ? `${rerun[1]} rerun` : null;
  }
  const round = event.kind === "check_fix_round" ? CHECK_FIX_ROUND.exec(event.summary) : null;
  if (round == null) return null;
  const at = events.indexOf(event);
  const since = events.findLastIndex((other, index) => index < at && other.kind === "babysit_step");
  const again = events.some((other, index) => index > since && index < at && other.kind === "check_rerun") ? " again" : "";
  const failing = event.summary.slice(round[0].length).trim();
  return `${round[3]} failed${again}${failing ? ` · ${failing}` : ""} → CI fix round ${round[1]} of ${round[2]}`;
}

function eventSteps(detail: RunDetail, events: RunEvent[], used: Set<RunEvent>): ActivityStep[] {
  return events
    .filter((event) => !used.has(event) && !NEVER_STEPS.has(event.kind))
    .map((event) => {
      const check = checkText(event, events);
      return { at: event.at, text: check ?? summarize(event, detail.rounds), ms: null, quiet: check == null };
    });
}

function askSteps(detail: RunDetail, asks: ConsoleAsk[]): ActivityStep[] {
  const { outcome, ended_ms: ended } = detail.run;
  return asks
    .filter((ask) => outcome == null || ended == null || ask.asked_ms <= ended)
    .map((ask) => ({ at: ask.asked_ms, text: `You asked: ${ask.question}`, ms: null, quiet: false }));
}

function place(groups: Draft[], steps: ActivityStep[]) {
  for (const step of steps) {
    const open = groups.findLast((group) => group.at <= step.at) ?? groups[0];
    open?.steps.push(step);
  }
  for (const group of groups) group.steps.sort((a, b) => a.at - b.at);
}

function nowLine(group: ActivityGroup & { what?: string }, events: RunEvent[]): ActivityNow {
  const progress = events.findLast((event) => event.kind === "turn_progress" && event.at >= group.at);
  return { what: group.what ?? group.title, since: group.at, detail: progress?.summary ?? group.steps.at(-1)?.text ?? null };
}

/**
 * A `/runs/N` answer as the timeline's ordered groups — notes carried in,
 * implement, each review round with its verify, review and fix, the final
 * check, the pull request and each babysit pass — with every other event,
 * note and ask a step of the group open at its time. A live run's last
 * group is running, and `now` says what it is doing.
 */
export function runActivity(detail: RunDetail, asks: ConsoleAsk[], now: number): RunActivity {
  const events = orderEvents(detail.events);
  const used = new Set<RunEvent>();
  const notes = runNotes(detail);
  const babysat = babysitRounds(detail, events);
  const carried = notes.filter((note) => note.consumed_by === detail.run.id && note.at < detail.run.started_ms);
  const drafts = [
    ...implementGroup(detail, events, used),
    ...roundGroups(detail, events, babysat, used),
    ...pullRequestGroups(events, used),
    ...babysitGroups(events, babysat, used),
  ].sort((a, b) => a.at - b.at);
  const during = notes.filter((note) => !carried.includes(note));
  place(drafts, [...eventSteps(detail, events, used), ...during.map(noteStep), ...askSteps(detail, asks)]);
  const all = [...beforeGroup(carried), ...drafts];
  const live = detail.run.outcome == null;
  const groups = all.map((group, index): ActivityGroup & { what?: string } => {
    const { end, ...rest } = group;
    const running = live && index === all.length - 1;
    const close = running || group.kind === "before" ? null : (end ?? all[index + 1]?.at ?? detail.run.ended_ms ?? now);
    return { ...rest, ms: close == null ? null : close - group.at, running, tone: running ? "live" : group.tone };
  });
  const last = groups.at(-1);
  return { groups: groups.map(({ what: _what, ...group }) => group), now: live && last != null ? nowLine(last, events) : null };
}

export interface GridCell {
  status: RoundCriterion["status"];
  note: string;
}

interface GridColumn {
  label: string;
  /** The final check's column when that check did not pass. */
  failed: boolean;
}

export interface CriteriaGrid {
  columns: GridColumn[];
  /** One row per criterion id in order, a cell per column; null where the column did not judge it. */
  rows: { id: string; cells: (GridCell | null)[] }[];
}

/**
 * The run's criteria by round: the witness pre-check, each review round
 * that judged criteria, and the final check; null when none did.
 */
export function criteriaGrid(detail: RunDetail): CriteriaGrid | null {
  const events = orderEvents(detail.events);
  const judged: { column: GridColumn; cells: Map<string, GridCell>; order: Map<string, number> }[] = [];
  const precheck = detail.precheck?.criteria ?? [];
  if (precheck.length > 0) {
    judged.push({
      column: { label: "pre-check", failed: false },
      cells: new Map(precheck.map((entry) => [entry.id, { status: entry.witnessed ? "met" : "unwitnessed", note: entry.note }])),
      order: new Map(precheck.map((entry) => [entry.id, entry.n])),
    });
  }
  const babysat = babysitRounds(detail, events);
  const rounds = detail.rounds.filter((round) => round.criteria.length > 0 && !babysat.has(round)).sort((a, b) => a.round - b.round);
  for (const round of rounds) {
    const final = round.round > detail.run.max_rounds || isFinalCheck(round, events);
    judged.push({
      column: { label: final ? "final" : `round ${round.round}`, failed: final && round.ended_ms != null && !PASSED.has(round.verdict) },
      cells: new Map(round.criteria.map((criterion) => [criterion.id, { status: criterion.status, note: criterion.note }])),
      order: new Map(round.criteria.map((criterion) => [criterion.id, criterion.n])),
    });
  }
  if (judged.length === 0) return null;
  const order = new Map(judged.flatMap((each) => [...each.order]));
  const ids = [...order.keys()].sort((a, b) => order.get(a)! - order.get(b)!);
  return {
    columns: judged.map((each) => each.column),
    rows: ids.map((id) => ({ id, cells: judged.map((each) => each.cells.get(id) ?? null) })),
  };
}
