import { useContext } from "react";
import { useQuery } from "@tanstack/react-query";
import { scoutDetailSchema, type ScoutDetail, type ScoutFinding, type ScoutReportPass, type ScoutTask } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { AnswerError, POLL_INTERVAL_MS, type Fetch } from "../lib/poll";
import type { ActivityStep, ActivityTone } from "./activity";
import { firstLine } from "./history";
import { fetchRoute, routeKey } from "./queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { followUps, livePass, type FollowUp } from "./scoutThread";
import { scoutState, sentence, type ScoutState } from "./scouts";
import type { State } from "./state";

export type ScoutPage =
  | { kind: "loading" }
  | { kind: "unknown_project" }
  | { kind: "not_found" }
  /** The first read failed with no earlier answer; `status` is the daemon's non-2xx code, null for any other failure. */
  | { kind: "failed"; status: number | null }
  | {
      kind: "scout";
      base: string;
      detail: ScoutDetail;
      actions: boolean;
      now: number;
      state: ScoutState;
      /** Whether the scout is planning, swarming or synthesizing, so its Progress may grow. */
      live: boolean;
      stoppable: boolean;
      sentence: string;
      stage: { label: string; sub: string | null };
      activity: ScoutGroup[];
      /** The newest follow-up pass, its span counted to now while it runs. */
      pass: { pass: number; ms: number | null; live: boolean } | null;
    };

/** The scout states drawn with a run state's dot. */
export const SCOUT_DOTS: Record<ScoutState, State> = { running: "running", queued: "queued", done: "merged", stopped: "abandoned", failed: "failed" };

/** Scout `id`'s page, on the project at path `project`, as a `Link` target that keeps the sidebar's scope. */
export function scoutPage(project: string, id: number) {
  return { to: "/scouts/$project/$scout", params: { project: projectName(project), scout: String(id) }, search: keepScope } as const;
}

type ScoutGroupKind = "plan" | "wave" | "synthesis" | "report" | "next" | "answer" | "pass";

export interface ScoutGroup {
  /** Stable across polls: "plan", "wave:1:2", "synthesis:1", "report", "next", "answer:AT", "pass:P:AT". */
  key: string;
  kind: ScoutGroupKind;
  /** Null for the pending row after a live scout's last wave. */
  at: number | null;
  title: string;
  sub: string[];
  /** Null while running and for the pending row. */
  ms: number | null;
  tone: ActivityTone;
  running: boolean;
  steps: ActivityStep[];
  tasks: ScoutTask[];
}

export interface NumberedFinding {
  id: string;
  pass: number;
  finding: ScoutFinding;
}

/** Every pass's findings in pass order, numbered F1… across the passes. */
export function numberFindings(reports: ScoutReportPass[]): NumberedFinding[] {
  return [...reports].sort((a, b) => a.pass - b.pass)
    .flatMap(({ pass, report }) => report.findings.map((finding) => ({ pass, finding })))
    .map((each, index) => ({ ...each, id: `F${index + 1}` }));
}

const ENDED = new Set<ScoutTask["phase"]>(["done", "failed", "stopped"]);
const BACK = new Set<ScoutTask["phase"]>(["done", "failed"]);

export const backCount = (tasks: ScoutTask[]) => tasks.filter((task) => BACK.has(task.phase)).length;
export const stoppedCount = (tasks: ScoutTask[]) => tasks.filter((task) => task.phase === "stopped").length;

/** A token count in thousands: "38k tok". */
export const tokenWords = (tokens: number) => `${Math.round(tokens / 1000)}k tok`;

export const isLive = (phase: ScoutDetail["scout"]["phase"]) => phase === "planning" || phase === "swarming" || phase === "synthesizing";

interface Wave {
  wave: number;
  tasks: ScoutTask[];
  start: number | null;
  end: number | null;
  /** Whether any helper of the wave started or every one ended. */
  launched: boolean;
}

/** Pass 1's tasks by wave, in wave order, each in plan order. A wave of an
 *  ended scout (`endedMs`) whose started tasks never all ended ends with the
 *  scout; once the lead is `synthesizing`, a later wave has launched, or the
 *  scout ended with only never-started tasks left, a wave ends with its last
 *  task back. */
function wavesOf(tasks: ScoutTask[], endedMs: number | null = null, synthesizing = false): Wave[] {
  const byWave = new Map<number, ScoutTask[]>();
  for (const task of tasks) {
    if (task.pass !== 1) continue;
    byWave.set(task.wave, [...(byWave.get(task.wave) ?? []), task]);
  }
  const waves = [...byWave.entries()]
    .sort(([a], [b]) => a - b)
    .map(([wave, each]) => {
      const starts = each.flatMap((task) => (task.started_ms == null ? [] : [task.started_ms]));
      const ends = each.flatMap((task) => (ENDED.has(task.phase) && task.ended_ms != null ? [task.ended_ms] : []));
      return { wave, each, starts, ends, launched: starts.length > 0 || ends.length === each.length };
    });
  return waves.map(({ wave, each, starts, ends, launched }, index) => {
    if (!launched) return { wave, tasks: each, start: null, end: null, launched };
    const followed = waves.slice(index + 1).some((later) => later.launched);
    const cut = endedMs != null && each.every((task) => (ENDED.has(task.phase) && task.ended_ms != null) || task.started_ms == null);
    const end = ends.length && (ends.length === each.length || synthesizing || followed || cut) ? Math.max(...ends) : endedMs;
    return { wave, tasks: each, start: starts.length ? Math.min(...starts) : null, end, launched };
  });
}

function waveTone(tasks: ScoutTask[], running: boolean): ActivityTone {
  if (running) return "live";
  if (tasks.some((task) => !ENDED.has(task.phase) || task.phase === "stopped")) return "wait";
  if (tasks.some((task) => task.phase === "failed")) return "bad";
  return "ok";
}

const ENDED_TONE: Partial<Record<ScoutDetail["scout"]["phase"], ActivityTone>> = { failed: "bad", stopped: "wait" };

function waveGroup(wave: Wave, helper: string | null, swarm: number | null, running: boolean, at: number): ScoutGroup {
  const { tasks } = wave;
  const width = Math.min(swarm ?? tasks.length, tasks.length);
  const stopped = stoppedCount(tasks);
  return {
    key: `wave:1:${wave.wave}`,
    kind: "wave",
    at: wave.start ?? at,
    title: `Wave ${wave.wave}`,
    sub: [...(helper == null ? [] : [`${helper} ×${width}`]), `${backCount(tasks)} of ${tasks.length} back`, ...(stopped ? [`${stopped} stopped`] : [])],
    ms: running || !wave.launched || wave.start == null || wave.end == null ? null : wave.end - wave.start,
    tone: waveTone(tasks, running),
    running,
    steps: [],
    tasks,
  };
}

interface Span {
  key: string;
  kind: "plan" | "synthesis" | "report";
  title: string;
  sub: string[];
  start: number;
  /** Null while it runs. */
  end: number | null;
  tone: ActivityTone;
}

const subTasks = (n: number, more: string) => `${n} ${more}${n === 1 ? "sub-task" : "sub-tasks"}`;

/** A wave no helper of which ever started, once the scout is past swarming,
 *  sits at the end of the group before it and takes no time. */
function pinUnlaunched(waves: Wave[], planEnd: number) {
  let edge = planEnd;
  for (const wave of waves) {
    if (!wave.launched) wave.start = wave.end = edge;
    edge = wave.end ?? edge;
  }
}

/** Plan, each wave with Synthesis between two, and Report, as the timeline's
 *  groups; a live scout's last group runs, and a pending row follows a running wave. */
function scoutActivity(detail: ScoutDetail, now: number): ScoutGroup[] {
  if (detail.scout.started_ms == null) return [];
  const later = followUps(detail);
  const first = later.length ? { ...detail, scout: { ...detail.scout, phase: "done" as const } } : detail;
  return [...firstRun(first, now), ...later.map((each) => followUpGroup(detail, each, now))];
}

function firstRun(detail: ScoutDetail, now: number): ScoutGroup[] {
  const { scout, events } = detail;
  if (scout.started_ms == null) return [];
  const live = isLive(scout.phase);
  const lead = scout.lead == null ? [] : [scout.lead];
  const waves = wavesOf(detail.tasks, live ? null : scout.ended_ms, scout.phase === "synthesizing");
  const firstTurn = events.find((event) => event.kind === "agent_turn" && event.at >= scout.started_ms!)?.at ?? null;
  const planEnd = waves[0]?.start ?? waves[0]?.end ?? (scout.phase === "planning" ? null : (firstTurn ?? scout.ended_ms));
  if (scout.phase !== "planning" && scout.phase !== "swarming") pinUnlaunched(waves, planEnd ?? scout.started_ms);
  const firstWave = waves[0]?.tasks.length ?? 0;
  const endTone = ENDED_TONE[scout.phase] ?? "ok";
  const spans: (Span | Wave)[] = [{
    key: "plan", kind: "plan", title: "Plan", sub: [...lead, ...(firstWave ? [subTasks(firstWave, "")] : [])],
    start: scout.started_ms, end: planEnd, tone: waves.length || firstTurn != null ? "ok" : endTone,
  }];
  waves.forEach((wave, index) => {
    const next = waves[index + 1];
    spans.push(wave);
    if (next == null) return;
    spans.push({
      key: `synthesis:${wave.wave}`, kind: "synthesis", title: "Synthesis", sub: [...lead, subTasks(next.tasks.length, "more ")],
      start: wave.end ?? next.start ?? now, end: next.start ?? (live ? null : scout.ended_ms), tone: "ok",
    });
  });
  const last = waves.at(-1);
  const synthesizing = scout.phase === "synthesizing";
  const tail = last == null ? planEnd : last.end;
  const wavesLeft = waves.length < (scout.budget.waves ?? 1);
  if (tail != null && (synthesizing || (!live && scout.ended_ms != null && scout.ended_ms > tail))) {
    const synthesis = synthesizing && waves.length > 0 && wavesLeft;
    spans.push({
      key: synthesis ? `synthesis:${last?.wave ?? 0}` : "report",
      kind: synthesis ? "synthesis" : "report",
      title: synthesis ? "Synthesis" : "Report",
      sub: [...lead, ...(synthesizing ? [] : reportWords(detail))],
      start: tail, end: synthesizing ? null : scout.ended_ms, tone: endTone,
    });
  }
  const groups = spans.map((span, index): ScoutGroup => {
    if ("wave" in span) {
      const running = live && span === last && span.end == null;
      return waveGroup(span, scout.helper, scout.swarm, running, spans[index - 1]?.end ?? now);
    }
    const running = span.end == null && live && index === spans.length - 1;
    return {
      key: span.key, kind: span.kind, at: span.start, title: span.title, sub: span.sub,
      ms: running || span.end == null ? null : span.end - span.start,
      tone: running ? "live" : span.tone, running, tasks: [],
      steps: events
        .filter((event) => (span.kind === "plan" ? event.at >= span.start : event.at > span.start) && event.at <= (span.end ?? now))
        .map((event) => ({ at: event.at, text: event.summary, ms: null, quiet: true })),
    };
  });
  if (live && last != null && groups.at(-1)?.kind === "wave") {
    groups.push({
      key: "next", kind: "next", at: null, title: wavesLeft ? "Synthesis" : "Report", sub: lead,
      ms: null, tone: "neutral", running: false, steps: [], tasks: [],
    });
  }
  return groups;
}

const counted = (findings: number, actions: number) => [
  ...(findings ? [`${findings} ${findings === 1 ? "finding" : "findings"}`] : []),
  ...(actions ? [`${actions} ${actions === 1 ? "action item" : "action items"}`] : []),
];

/** The first run's report, in counts. */
function reportWords(detail: ScoutDetail): string[] {
  const report = [...detail.reports].sort((a, b) => a.pass - b.pass)[0]?.report;
  return report == null ? [] : counted(report.findings.length, report.actions.length);
}

const ANSWER_TONE: Record<string, ActivityTone> = { scout_answered: "ok", scout_answer_failed: "bad", scout_answer_stopped: "wait" };

/** Pass `run`'s tasks: those of its pass that started inside its span, and
 *  while it runs, those not yet started. An earlier try at the same pass can
 *  leave tasks that never started, and helpers start in id order, so once a
 *  retry's first task starts, an unstarted task is the run's only above it;
 *  before one starts, the live run holds every unstarted task of its pass. */
function passTasks(detail: ScoutDetail, run: FollowUp, running: boolean): ScoutTask[] {
  const inside = (started: number) => started >= run.started.at && (run.end != null ? started <= run.end.at : started < (run.until ?? Infinity));
  const own = detail.tasks.filter((task) => task.pass === run.pass);
  const started = own.filter((task) => task.started_ms != null && inside(task.started_ms));
  if (!running) return started;
  const retry = followUps(detail).some((each) => each.kind === "pass" && each.pass === run.pass && each.started.at < run.started.at);
  const floor = retry && started.length ? Math.min(...started.map((task) => task.id)) : -Infinity;
  return own.filter((task) => (task.started_ms == null ? task.id > floor : started.includes(task)));
}

function followUpGroup(detail: ScoutDetail, run: FollowUp, now: number): ScoutGroup {
  const { scout } = detail;
  const at = run.started.at;
  const ms = run.end == null ? null : run.end.at - at;
  const lead = scout.lead == null ? [] : [scout.lead];
  if (run.kind === "answer") {
    const running = run.end == null && run.until == null && scout.phase === "answering";
    const end = run.end?.at ?? (running ? now : at);
    return {
      key: `answer:${at}`, kind: "answer", at, title: "Answer", sub: lead, ms,
      tone: running ? "live" : (ANSWER_TONE[run.end?.kind ?? ""] ?? "wait"), running, tasks: [],
      steps: detail.events.filter((event) => event.at > at && event.at <= end).map((event) => ({ at: event.at, text: event.summary, ms: null, quiet: true })),
    };
  }
  const running = livePass(detail)?.started === run.started;
  const tasks = passTasks(detail, run, running);
  const width = Math.min(scout.swarm ?? tasks.length, tasks.length);
  const stopped = stoppedCount(tasks);
  const report = detail.reports.find((each) => each.pass === run.pass)?.report;
  const items = detail.actions.filter((action) => action.pass === run.pass).length;
  return {
    key: `pass:${run.pass}:${at}`, kind: "pass", at, title: `Pass ${run.pass}`,
    sub: [
      ...(scout.helper == null || !tasks.length ? [] : [`${scout.helper} ×${width}`]),
      ...(tasks.length ? [`${backCount(tasks)} of ${tasks.length} back`] : []),
      ...(stopped ? [`${stopped} stopped`] : []),
      ...(run.end?.kind === "scout_pass_done" ? counted(report?.findings.length ?? 0, items) : []),
    ],
    ms,
    tone: run.end?.kind === "scout_pass_failed" ? "bad" : waveTone(tasks, tasks.some((task) => task.phase === "running")),
    running, steps: [], tasks,
  };
}

/** The newest follow-up pass, its span to its end or, while it runs, to now. */
function newestPass(detail: ScoutDetail, now: number) {
  const newest = followUps(detail).findLast((each) => each.kind === "pass");
  if (newest == null) return null;
  const live = livePass(detail) != null;
  const end = newest.end?.at ?? (live ? now : null);
  return { pass: newest.pass!, ms: end == null ? null : end - newest.started.at, live };
}

const are = (n: number, many: string, one: string) => (n === 1 ? one : many);

/** "5 of 10 helpers are back, 4 are running, and 1 waits for a slot" or "1 of 2 helpers are back, and 1 is
 *  running", leaving out a clause whose count is 0. */
function swarmClauses(tasks: ScoutTask[]): string {
  const count = (phase: ScoutTask["phase"]) => tasks.filter((task) => task.phase === phase).length;
  const parts = [
    { n: backCount(tasks), verb: (n: number) => are(n, "are back", "is back") },
    { n: count("running"), verb: (n: number) => are(n, "are running", "is running") },
    { n: count("queued"), verb: (n: number) => are(n, "wait for a slot", "waits for a slot") },
  ].filter((part) => part.n > 0);
  const clauses = parts.map((part, index) => (index === 0 ? `${part.n} of ${tasks.length} helpers ${part.verb(tasks.length)}` : `${part.n} ${part.verb(part.n)}`));
  if (clauses.length < 2) return clauses.join("");
  return `${clauses.slice(0, -1).join(", ")}, and ${clauses.at(-1)}`;
}

/** The sentence behind the scout's state dot. */
function scoutSentence(detail: ScoutDetail): string {
  const { scout } = detail;
  const pass = livePass(detail);
  if (pass != null) return passSentence(detail, pass);
  switch (scout.phase) {
    case "queued":
      return "Waiting for a scout slot.";
    case "planning":
      return "The lead is planning.";
    case "swarming": {
      const wave = wavesOf(detail.tasks).at(-1);
      const clauses = wave == null ? "" : swarmClauses(wave.tasks);
      return wave == null || clauses === "" ? "The lead is handing out sub-tasks." : `Wave ${wave.wave}: ${clauses}.`;
    }
    case "synthesizing":
      return "The lead is reading what came back.";
    case "answering":
      return "The lead is answering.";
    case "done":
      return detail.reports.at(-1)?.report.summary ?? "Done.";
    case "failed":
      return `Failed: ${firstLine(scout.reason)}`;
    case "stopped":
      return sentence(scout.reason ?? "stopped");
  }
}

function passSentence(detail: ScoutDetail, run: FollowUp): string {
  const head = `Pass ${run.pass}:`;
  if (detail.scout.phase === "planning") return `${head} the lead is planning.`;
  if (detail.scout.phase === "synthesizing") return `${head} the lead is writing its report.`;
  const clauses = swarmClauses(passTasks(detail, run, true));
  return clauses === "" ? `${head} the lead is handing out sub-tasks.` : `${head} ${clauses}.`;
}

/** The Stage chip: the phase, with the live pass, or its wave while swarming
 *  or synthesizing, or a done scout's highest report pass past the first. */
function scoutStage(detail: ScoutDetail): { label: string; sub: string | null } {
  const { scout } = detail;
  const pass = livePass(detail);
  if (pass != null) return { label: scout.phase, sub: `pass ${pass.pass}` };
  const highest = Math.max(0, ...detail.reports.map((report) => report.pass));
  if (scout.phase === "done" && highest > 1) return { label: scout.phase, sub: `pass ${highest}` };
  const wave = wavesOf(detail.tasks).at(-1);
  if ((scout.phase !== "swarming" && scout.phase !== "synthesizing") || wave == null) return { label: scout.phase, sub: null };
  const of = scout.budget.waves == null ? "" : ` of ${scout.budget.waves}`;
  return { label: scout.phase, sub: `wave ${wave.wave}${of}` };
}

/** `/scouts/N` at `base`, read again every `POLL_INTERVAL_MS` while mounted; idle with no scout. */
function useScout(base: string | null, id: number | null, deps: { fetch: Fetch }) {
  const path = `/scouts/${String(id)}`;
  return useQuery({
    queryKey: routeKey(base ?? "", path),
    queryFn: () => fetchRoute(deps.fetch, `${base}${path}`, scoutDetailSchema),
    enabled: base != null && id != null,
    refetchInterval: POLL_INTERVAL_MS,
    staleTime: POLL_INTERVAL_MS,
  });
}

/** Scout `id` of the one host whose project is named `project`, with the
 *  words its header and Progress say it in. */
export function useScoutPage(project: string, id: number | null): ScoutPage {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { now } = peers;
  const host = scopedHosts(peers.hosts, project)[0] ?? null;
  const answer = useScout(host?.base ?? null, id, deps);
  if (host == null) return peers.polls > 0 ? { kind: "unknown_project" } : { kind: "loading" };
  if (id == null || (answer.error instanceof AnswerError && answer.error.status === 404)) return { kind: "not_found" };
  const status = host.contract_error ? undefined : host.status;
  const detail = answer.data;
  if (detail == null && answer.error != null) {
    return { kind: "failed", status: answer.error instanceof AnswerError ? answer.error.status : null };
  }
  if (detail == null || status == null) return { kind: "loading" };
  const { phase } = detail.scout;
  return {
    kind: "scout",
    base: host.base,
    detail,
    actions: status.actions === true,
    now,
    state: scoutState(phase),
    live: isLive(phase),
    stoppable: !["done", "failed", "stopped"].includes(phase),
    sentence: scoutSentence(detail),
    stage: scoutStage(detail),
    activity: scoutActivity(detail, now),
    pass: newestPass(detail, now),
  };
}
