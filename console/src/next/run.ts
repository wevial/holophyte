import { useContext } from "react";
import { describe, needsYouEntries } from "../lib/attention";
import type { RouteLabels, RunDetail, Status, StatusRun } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { formatClock, formatSettled } from "../lib/format";
import { hostItems, sinceSeen } from "../lib/hosts";
import { AnswerError } from "../lib/poll";
import { agentMs, phaseLabel, runKey } from "../lib/runs";
import { prLabel, shortSha } from "../lib/shipped";
import type { AttentionItem } from "../lib/types";
import { failureWord } from "./history";
import { useReadiness, useRun } from "./queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { stateOf, type State } from "./state";

export type RunPage =
  | { kind: "loading" }
  | { kind: "unknown_project" }
  | { kind: "not_found" }
  | {
      kind: "run";
      detail: RunDetail;
      state: State;
      /** The run's agent clock and its box, both null when the daemon gave none. */
      agentMs: number | null;
      boxMs: number | null;
      labels: RouteLabels | null;
      sentence: string;
      stage: { label: string; sub: string | null };
    };

/** Run `id`'s page, on the project at path `project`, as a `Link` target
 *  that keeps the sidebar's scope. */
export function runPage(project: string, id: number) {
  return { to: "/runs/$project/$run", params: { project: projectName(project), run: String(id) }, search: keepScope } as const;
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

function firstLine(text: unknown): string {
  return typeof text === "string" ? (text.split("\n", 1)[0] ?? "").trim() : "";
}

/** `head`, then `: detail` with one closing full stop, or `head.` alone. */
function clause(head: string, detail: string): string {
  return detail ? `${head}: ${detail.replace(/[.\s]+$/, "")}.` : `${head}.`;
}

const STEPS: Record<string, { stage: string; doing: string }> = {
  working: { stage: "Implement", doing: "Implementing" },
  addressing: { stage: "Implement", doing: "Implementing" },
  verifying: { stage: "Verify", doing: "Verifying" },
  reviewing: { stage: "Review", doing: "Reviewing" },
  merge_gate: { stage: "Merge", doing: "Merging" },
  merging: { stage: "Merge", doing: "Merging" },
  squashing: { stage: "Merge", doing: "Merging" },
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

function liveWords({ live, phase }: Facts): { stage: string; sentence: string } {
  const step = STEPS[phase] ?? { stage: capital(phaseLabel(phase)), doing: capital(phaseLabel(phase)) };
  const round = phase === "reviewing" && live != null ? `round ${live.round + 1} of ${live.max_rounds}` : null;
  const stop = live?.stop_requested == null ? null : clause(live.stop_action === "pause" ? "Pause requested" : "Abort requested", live.stop_requested);
  return {
    stage: round == null ? step.stage : `${step.stage} · ${round}`,
    sentence: stop ?? (round == null ? `${step.doing}.` : `${step.doing} ${round}.`),
  };
}

/** The run's stage chip and the sentence before "Nothing needs you.". */
function stageWords(facts: Facts): { stage: string; sentence: string } {
  const { detail, item, now } = facts;
  const run = detail.run;
  const pr = run.pr_url == null ? "its PR" : prLabel(run.pr_url);
  switch (facts.state) {
    case "on_ci":
      return { stage: `On CI · ${pr}`, sentence: `Waiting on CI for ${pr}.` };
    case "ready_to_merge":
      return { stage: `Ready for your merge · ${pr}`, sentence: `${capital(pr)} is ready for your merge.` };
    case "parked": {
      const blocker = item == null ? undefined : describe(item, facts.thresholds, { now }).facts?.find((fact) => fact.tone !== "ok");
      return { stage: `Parked · ${pr}`, sentence: clause(`Parked on ${pr}`, blocker?.label ?? "") };
    }
    case "stale": {
      const silent = formatSettled(facts.heartbeatMs);
      return { stage: `Stale · no beat ${silent}`, sentence: `No heartbeat for ${silent}. It is up in Needs you.` };
    }
    case "question":
      return { stage: "Asks you", sentence: clause("It asks you", firstLine(item?.question)) };
    case "paused":
      return { stage: "Paused", sentence: `${clause("Paused", firstLine(item?.note))} It waits for you to resume it.` };
    case "failed": {
      const word = capital(failureWord(run.failure_kind));
      return { stage: word, sentence: clause(word, firstLine(run.outcome_reason)) };
    }
    case "abandoned":
      return { stage: "Abandoned", sentence: clause("Abandoned", firstLine(run.outcome_reason)) };
    case "merged": {
      const sha = shortSha(run.merge_sha);
      const at = run.ended_ms == null ? "" : ` at ${formatClock(run.ended_ms)}`;
      return { stage: sha ? `Merged · ${sha}` : "Merged", sentence: `Merged to main${sha ? ` as ${sha}` : ""}${at}.` };
    }
    default:
      return liveWords(facts);
  }
}

/** States whose own sentence already says the run waits on the maintainer. */
const ASKS_YOU = new Set<State>(["stale", "question", "paused", "ready_to_merge"]);

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
  if (host == null) return peers.polls > 0 ? { kind: "unknown_project" } : { kind: "loading" };
  if (run == null || (answer.error instanceof AnswerError && answer.error.status === 404)) return { kind: "not_found" };
  const status = host.contract_error ? undefined : host.status;
  const detail = answer.data;
  if (detail == null || status == null) return { kind: "loading" };

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
  const needsYou = ASKS_YOU.has(state) || needsYouEntries(items, readiness, now).length > 0;
  const words = stageWords({ detail, live, phase, state, item, thresholds: status.thresholds, heartbeatMs: heartbeat ?? 0, now });
  const loopsBack = detail.rounds.filter((round) => round.verdict === "changes_requested").length;
  return {
    kind: "run",
    detail,
    state,
    agentMs: live == null ? agentMs(detail.run) : agentMs(live, since),
    boxMs: live?.time_box_ms ?? detail.run.time_box_ms,
    labels: status.route_labels,
    sentence: needsYou ? words.sentence : `${words.sentence} Nothing needs you.`,
    stage: {
      label: words.stage,
      sub: state === "running" && loopsBack > 0 ? `${loopsBack} loop${loopsBack === 1 ? "" : "s"} back` : null,
    },
  };
}
