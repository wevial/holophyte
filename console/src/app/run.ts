import { useContext } from "react";
import { describe, type Fact } from "../lib/attention";
import type { ConsoleAsk, Round, RouteLabels, RunCriterion, RunDetail, RunTurn, Status, StatusRun } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { formatClock, formatSettled } from "../lib/format";
import { edge, orderEvents } from "../lib/log";
import { hostItems, sinceSeen, type HostRecord } from "../lib/hosts";
import { AnswerError } from "../lib/poll";
import { agentMs, phaseLabel, runKey } from "../lib/runs";
import { prLabel, shortSha } from "../lib/shipped";
import type { AttentionItem } from "../lib/types";
import { PASSED, runActivity, type RunActivity } from "./activity";
import type { ContractBodies } from "./fleet";
import { failureWord } from "./history";
import { progressLines, type ProgressLine } from "./progress";
import { useReadiness, useRun, useRunAsks } from "./queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { roundLabel } from "./components/StageChip";
import { stateOf, type State } from "./state";

export type RunPage =
  | { kind: "loading" }
  | { kind: "unknown_project" }
  | { kind: "not_found" }
  | {
      kind: "run";
      /** The base of the host that serves the run. */
      base: string;
      detail: RunDetail;
      host: HostRecord<ContractBodies>;
      liveRun: StatusRun | null;
      liveTicket: boolean;
      actions: boolean;
      stops: string;
      state: State;
      /** The run's agent clock and its box, both null when the daemon gave none. */
      agentMs: number | null;
      boxMs: number | null;
      labels: RouteLabels | null;
      /** What its `pr_open` item says of its pull request; null with no such item. */
      prFacts: Fact[] | null;
      sentence: string;
      progress: ProgressLine[];
      activity: RunActivity;
      asks: ConsoleAsk[];
      agentState: string;
      phase: string;
      stoppable: boolean;
      /** Whether the run is still under way, so its Progress may grow. */
      live: boolean;
    };

export function agentState(run: { phase: string; outcome: string | null; round: number }): string {
  if (run.outcome != null || ["done", "failed", "killed", "rejected", "paused"].includes(run.phase)) return "stopped with the run";
  if (run.phase === "verifying") return "idle while verify runs";
  if (run.phase === "reviewing") return `idle until review round ${run.round + 1} returns`;
  if (run.phase === "awaiting_merge_approval") return "idle while the pull request waits";
  return "working";
}

/** Run `id`'s page, on the project at path `project`, as a `Link` target
 *  that keeps the sidebar's scope. */
export function runPage(project: string, id: number) {
  return { to: "/runs/$project/$run", params: { project: projectName(project), run: String(id) }, search: keepScope } as const;
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

const NUMBER_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
const numberWord = (n: number) => NUMBER_WORDS[n] ?? String(n);

export function criteriaClause(criteria: RunCriterion[]): string {
  if (!criteria.length) return "";
  const met = criteria.filter((criterion) => criterion.status === "met").length;
  const total = numberWord(criteria.length);
  const noun = criteria.length === 1 ? "criterion is" : "criteria are";
  if (met === criteria.length) return `All ${total} ${noun} met.`;
  if (met === 0) return `None of ${total} ${criteria.length === 1 ? "criterion" : "criteria"} is met yet.`;
  return `${capital(numberWord(met))} of ${total} ${noun} met.`;
}

function firstLine(text: unknown): string {
  return typeof text === "string" ? (text.split("\n", 1)[0] ?? "").trim() : "";
}

/** `head`, then `: detail` with one closing full stop, or `head.` alone. */
function clause(head: string, detail: string): string {
  return detail ? `${head}: ${detail.replace(/[.\s]+$/, "")}.` : `${head}.`;
}

const DOING: Record<string, string> = {
  working: "Implementing",
  addressing: "Implementing",
  verifying: "Verifying",
  reviewing: "Reviewing",
  merge_gate: "Merging",
  merging: "Merging",
  squashing: "Merging",
};

interface Facts {
  detail: RunDetail;
  live: StatusRun | null;
  phase: string;
  state: State;
  item: AttentionItem | null;
  thresholds: Status["thresholds"];
  heartbeatMs: number;
  now: number;
}

const doingOf = (phase: string) => DOING[phase] ?? capital(phaseLabel(phase));

/** What an abort stops, said the way the verdict says the step. */
function stopsWord(phase: string, live: StatusRun | null): string {
  return phase === "reviewing" && live != null ? `Review round ${live.round + 1}` : doingOf(phase);
}

export const CHECK_FIX_ROUND = /^check fix round (\d+) of (\d+) on \S+: (.+?); failing: /;

function checkFixRound(events: RunDetail["events"]): string | null {
  const ordered = orderEvents(events);
  const steps = ordered.filter((event) => event.kind === "babysit_step");
  if (steps.at(-1)?.summary !== "fix") return null;
  const since = ordered.findLastIndex((event) => event.kind === "babysit_step" && event.summary !== "fix");
  const at = ordered.findLastIndex((event) => event.kind === "check_fix_round");
  const match = at > since ? CHECK_FIX_ROUND.exec(ordered[at]!.summary) : null;
  return match == null ? null : `${match[3]} failing (check fix round ${match[1]} of ${match[2]})`;
}

export type StageState = "finished" | "current" | "failed" | "not reached";
export type MarkGlyph = "loop" | "pass" | "running" | "failed" | "ended";

export interface StepperMark {
  label: string;
  glyph: MarkGlyph;
  card: { title: string; lines: string[]; model: string | null };
  /** The Progress anchor the mark links to; null for a mark with none. */
  anchor: string | null;
  /** Labels of the turns after this mark and before the next. */
  subs: string[];
}

export interface StepperStage {
  name: string;
  label: string;
  state: StageState;
  marks: StepperMark[];
  slots: number;
}

const STAGE_NAMES = ["Implement", "Verify", "Review", "Merge gate", "Merged"] as const;
const STAGE_OF: Record<string, number> = {
  claimed: 0,
  working: 0,
  verifying: 1,
  reviewing: 2,
  merge_gate: 3,
  merging: 3,
  squashing: 3,
  awaiting_merge_approval: 3,
};
const FAILED_STAGE: Record<string, number> = { verify: 1, review_route: 2, merge_lock: 3 };
const BLOCKER = /^p[01]$/i;

function phaseStage(phase: string, rounds: number): number | null {
  if (phase === "addressing") return rounds > 0 ? 2 : 0;
  return STAGE_OF[phase] ?? null;
}

/** Where the run stands: its phase's stage, else, once it has left the
 *  loop, the stage of the newest phase change that names one. */
function stageIndex(detail: RunDetail, phase: string): number {
  if (detail.run.outcome === "merged") return STAGE_NAMES.length;
  const rounds = detail.rounds.length;
  const here = phaseStage(phase, rounds);
  if (here != null) return here;
  for (const event of orderEvents(detail.events).reverse()) {
    const walked = event.kind === "phase_change" ? edge(event.summary) : null;
    const named = walked == null ? null : (phaseStage(walked[1], rounds) ?? phaseStage(walked[0], rounds));
    if (named != null) return named;
  }
  return FAILED_STAGE[detail.run.failure_kind ?? ""] ?? 0;
}

const plural = (count: number, noun: string) => `${count} ${noun}${count === 1 ? "" : "s"}`;

function roundMark(round: Round, next: Round | undefined, turns: RunTurn[], ended: boolean): StepperMark {
  const open = round.ended_ms == null;
  const glyph: MarkGlyph = round.verdict === "changes_requested" ? "loop" : PASSED.has(round.verdict) ? "pass" : open ? (ended ? "failed" : "running") : "ended";
  const verdict = open && glyph !== "failed" ? "in review" : round.verdict.replaceAll("_", " ") || "no verdict";
  const blockers = round.findings.filter((finding) => BLOCKER.test(finding.severity)).length;
  const first = round.findings[0];
  const between = next == null || round.ended_ms == null ? [] : turns.filter((turn) => turn.ended_ms > round.ended_ms! && turn.ended_ms < next.started_ms);
  const fix = between.findIndex((turn) => turn.role === "implement");
  return {
    label: `Round ${round.round}: ${verdict}`,
    glyph,
    card: {
      title: `Round ${round.round} · ${verdict}`,
      lines: open ? [] : [`${plural(round.findings.length, "finding")} · ${plural(blockers, "blocker")}`, ...(first == null ? [] : [first.title ?? firstLine(first.message)])],
      model: round.reviewer_model,
    },
    anchor: `round-${round.round}`,
    subs: between.filter((_, index) => index !== fix).map((turn) => turn.label || turn.role),
  };
}

function checkFixMarks(events: RunDetail["events"], fixing: boolean, merged: boolean): StepperMark[] {
  const found = orderEvents(events).flatMap((event) => {
    const match = event.kind === "check_fix_round" ? CHECK_FIX_ROUND.exec(event.summary) : null;
    return match == null ? [] : [{ round: match[1]!, of: match[2]!, checks: match[3]! }];
  });
  return found.map(({ round, of, checks }, index) => {
    const latest = index === found.length - 1;
    const label = `Check fix round ${round} of ${of}`;
    return {
      label,
      glyph: !latest ? "failed" : fixing ? "running" : merged ? "pass" : "ended",
      card: { title: label, lines: [`failing: ${checks}`], model: null },
      anchor: null,
      subs: [],
    };
  });
}

/** The round a reviewing run is in before its row exists, as rounds are
 *  recorded only once they end: begun at the newest change into review. */
function roundUnderway(round: number, events: RunDetail["events"]): Round {
  const begun = orderEvents(events).findLast((event) => event.kind === "phase_change" && edge(event.summary)?.[1] === "reviewing");
  return {
    round, started_ms: begun?.at ?? Number.MAX_SAFE_INTEGER, ended_ms: null, verdict: "", reviewer_model: null,
    findings: [], instructions: [], operator_notes: [], criteria: [],
  };
}

/** The Run page's stepper: five fixed stages, the review rounds as marks
 *  under Review and the check fix rounds under Merge gate. */
export function stepperOf(detail: RunDetail, turns: RunTurn[], phase: string): StepperStage[] {
  const { run, rounds } = detail;
  const at = stageIndex(detail, phase);
  const underway = phase === "reviewing" && (rounds.length === 0 || rounds.at(-1)!.ended_ms != null);
  const shown = underway ? [...rounds, roundUnderway(rounds.length + 1, detail.events)] : rounds;
  const reviewLabel = at < 2 ? "Review" : at > 2 ? (rounds.length > 0 ? `Review · ${plural(rounds.length, "round")}` : "Review") : `Review · round ${shown.length} / ${run.max_rounds}`;
  const fixing = phase === "merge_gate" && checkFixRound(detail.events) != null;
  return STAGE_NAMES.map((name, index) => {
    const state: StageState = index < at ? "finished" : index > at ? "not reached" : run.outcome === "failed" ? "failed" : "current";
    if (name === "Review") {
      const marks = shown.map((each, n) => roundMark(each, shown[n + 1], turns, run.outcome != null));
      return { name, label: reviewLabel, state, marks, slots: state === "current" ? Math.max(0, run.max_rounds - marks.length) : 0 };
    }
    const marks = name === "Merge gate" ? checkFixMarks(detail.events, fixing, run.outcome === "merged") : [];
    return { name, label: name, state, marks, slots: 0 };
  });
}

function liveSentence({ detail, live, phase }: Facts): string {
  const doing = doingOf(phase);
  const fixing = phase === "merge_gate" && live?.stop_requested == null ? checkFixRound(detail.events) : null;
  if (fixing != null) return `Fixing CI: ${fixing}.`;
  const round = phase === "reviewing" && live != null ? roundLabel(live.round + 1, live.max_rounds) : null;
  const stop = live?.stop_requested == null ? null : clause(live.stop_action === "pause" ? "Pause requested" : "Abort requested", live.stop_requested);
  return stop ?? (round == null ? `${doing}.` : `${doing} ${round}.`);
}

/** The sentence the run's header says. */
function verdictSentence(facts: Facts): string {
  const { detail, item, now } = facts;
  const run = detail.run;
  const pr = run.pr_url == null ? "its PR" : prLabel(run.pr_url);
  switch (facts.state) {
    case "on_ci":
      return `Waiting on CI for ${pr}.`;
    case "ready_to_merge":
      return `${capital(pr)} is ready for your merge.`;
    case "parked": {
      const blocker = item == null ? undefined : describe(item, facts.thresholds, { now }).facts?.find((fact) => fact.tone !== "ok");
      return clause(`Parked on ${pr}`, blocker?.label ?? "");
    }
    case "stale":
      return `No heartbeat for ${formatSettled(facts.heartbeatMs)}. It is up in Waiting on you.`;
    case "question":
      return clause("It asks you", firstLine(item?.question));
    case "paused":
      return `${clause("Paused", firstLine(item?.note))} It waits for you to resume it.`;
    case "failed":
      return clause(capital(failureWord(run.failure_kind)), firstLine(run.outcome_reason));
    case "abandoned":
      return clause("Abandoned", firstLine(run.outcome_reason));
    case "merged": {
      const sha = shortSha(run.merge_sha);
      const at = run.ended_ms == null ? "" : ` at ${formatClock(run.ended_ms)}`;
      return `Merged to main${sha ? ` as ${sha}` : ""}${at}.`;
    }
    default:
      return liveSentence(facts);
  }
}

const ITEM_KIND: Partial<Record<State, string>> = { parked: "pr_open", question: "blocked", paused: "paused" };

/** Run `run` of the one host whose project is named `project`: its
 *  `/runs/N`, its `/status` row while live, its attention item, and the
 *  words its header says them in. */
export function useRunPage(project: string, run: number | null): RunPage {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { now } = peers;
  const host = scopedHosts(peers.hosts, project)[0] ?? null;
  const readiness = useReadiness(host == null ? [] : [host], deps);
  const answer = useRun(host?.base ?? null, run, deps);
  const asks = useRunAsks(host?.base ?? null, run, deps);
  if (host == null) return peers.polls > 0 ? { kind: "unknown_project" } : { kind: "loading" };
  if (run == null || (answer.error instanceof AnswerError && answer.error.status === 404)) return { kind: "not_found" };
  const status = host.contract_error ? undefined : host.status;
  const detail = answer.data;
  if (detail == null || status == null || asks.isPending) return { kind: "loading" };

  const since = sinceSeen(host.seen_ms, now);
  const live = status.runs.find((each) => each.id === run) ?? null;
  const reported = live?.heartbeat_age_ms ?? detail.run.heartbeat_age_ms;
  const heartbeat = reported == null ? null : reported + since;
  const phase = live?.phase ?? detail.run.phase;
  const state = stateOf({
    run: { ...detail.run, phase, heartbeat_age_ms: heartbeat },
    staleMs: status.thresholds.heartbeat_stale_ms,
    readiness: readiness.get(runKey(host.key, run)),
  });
  const items = hostItems(host, now).filter((item) => item.run === run);
  const item = items.find((each) => each.kind === ITEM_KIND[state]) ?? null;
  const prItem = items.find((each) => each.kind === "pr_open");
  const said = verdictSentence({ detail, live, phase, state, item, thresholds: status.thresholds, heartbeatMs: heartbeat ?? 0, now });
  const criteria = ["running", "stale", "question", "on_ci", "parked", "ready_to_merge"].includes(state) ? criteriaClause(detail.criteria) : "";
  const sentence = [said, criteria].filter(Boolean).join(" ");
  const runAsks = asks.data?.asks ?? [];
  return {
    kind: "run",
    base: host.base,
    actions: status.actions === true,
    phase,
    stoppable: live != null && detail.run.outcome == null && !["awaiting_merge_approval", "paused", "done", "failed", "killed", "rejected"].includes(phase),
    detail,
    host,
    liveRun: live,
    liveTicket: status.runs.some((each) => each.ticket === detail.run.ticket),
    stops: stopsWord(phase, live),
    state,
    agentMs: live == null ? agentMs(detail.run) : agentMs(live, since),
    boxMs: live?.time_box_ms ?? detail.run.time_box_ms,
    labels: status.route_labels,
    prFacts: prItem == null ? null : (describe(prItem, status.thresholds, { now }).facts ?? []),
    sentence,
    asks: runAsks,
    agentState: agentState({ ...detail.run, phase, round: live?.round ?? detail.rounds.length }),
    progress: progressLines(detail, runAsks, now),
    activity: runActivity(detail, runAsks, now),
    live: detail.run.outcome == null,
  };
}
