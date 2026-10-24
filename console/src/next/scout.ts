import { useContext } from "react";
import { useQuery } from "@tanstack/react-query";
import { scoutDetailSchema, type ScoutDetail, type ScoutTask } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { AnswerError, POLL_INTERVAL_MS, type Fetch } from "../lib/poll";
import type { ActivityStep, ActivityTone } from "./activity";
import { firstLine } from "./history";
import { fetchRoute, routeKey } from "./queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { scoutState, sentence, type ScoutState } from "./scouts";
import type { State } from "./state";

export type ScoutPage =
  | { kind: "loading" }
  | { kind: "unknown_project" }
  | { kind: "not_found" }
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
    };

/** The scout states drawn with a run state's dot. */
export const SCOUT_DOTS: Record<ScoutState, State> = { running: "running", queued: "queued", done: "merged", stopped: "abandoned", failed: "failed" };

/** Scout `id`'s page, on the project at path `project`, as a `Link` target that keeps the sidebar's scope. */
export function scoutPage(project: string, id: number) {
  return { to: "/scouts/$project/$scout", params: { project: projectName(project), scout: String(id) }, search: keepScope } as const;
}

type ScoutGroupKind = "plan" | "wave" | "synthesis" | "report" | "next";

export interface ScoutGroup {
  /** Stable across polls: "plan", "wave:1:2", "synthesis:1", "report", "next". */
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

const ENDED = new Set<ScoutTask["phase"]>(["done", "failed", "stopped"]);
const BACK = new Set<ScoutTask["phase"]>(["done", "failed"]);

export const backCount = (tasks: ScoutTask[]) => tasks.filter((task) => BACK.has(task.phase)).length;
export const stoppedCount = (tasks: ScoutTask[]) => tasks.filter((task) => task.phase === "stopped").length;

/** A token count in thousands: "38k tok". */
export const tokenWords = (tokens: number) => `${Math.round(tokens / 1000)}k tok`;

const isLive = (phase: ScoutDetail["scout"]["phase"]) => phase === "planning" || phase === "swarming" || phase === "synthesizing";

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

function reportWords(detail: ScoutDetail): string[] {
  const report = detail.reports.at(-1)?.report;
  if (report == null) return [];
  const { findings, actions } = report;
  return [
    ...(findings.length ? [`${findings.length} ${findings.length === 1 ? "finding" : "findings"}`] : []),
    ...(actions.length ? [`${actions.length} ${actions.length === 1 ? "action item" : "action items"}`] : []),
  ];
}

const are = (n: number, many: string, one: string) => (n === 1 ? one : many);

/** "5 of 10 helpers are back, 4 are running, and 1 waits for a slot", leaving out a clause whose count is 0. */
function swarmClauses(tasks: ScoutTask[]): string {
  const count = (phase: ScoutTask["phase"]) => tasks.filter((task) => task.phase === phase).length;
  const parts = [
    { n: backCount(tasks), verb: (n: number) => are(n, "are back", "is back") },
    { n: count("running"), verb: (n: number) => are(n, "are running", "is running") },
    { n: count("queued"), verb: (n: number) => are(n, "wait for a slot", "waits for a slot") },
  ].filter((part) => part.n > 0);
  const clauses = parts.map((part, index) => (index === 0 ? `${part.n} of ${tasks.length} helpers ${part.verb(part.n)}` : `${part.n} ${part.verb(part.n)}`));
  if (clauses.length < 3) return clauses.join(" and ");
  return `${clauses.slice(0, -1).join(", ")}, and ${clauses.at(-1)}`;
}

/** The sentence behind the scout's state dot. */
function scoutSentence(detail: ScoutDetail): string {
  const { scout } = detail;
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

/** The Stage chip: the phase, with its wave while swarming or synthesizing. */
function scoutStage(detail: ScoutDetail): { label: string; sub: string | null } {
  const { scout } = detail;
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
  };
}
