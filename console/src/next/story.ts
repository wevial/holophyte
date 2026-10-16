import type { ReadinessMap } from "../lib/attention";
import type { StoryChildRow, StoryDetail } from "../lib/contract.gen";
import { formatClock } from "../lib/format";
import { hostItems, type HostRecord } from "../lib/hosts";
import { localMidnight } from "../lib/ledger";
import { runKey } from "../lib/runs";
import { stageWord } from "./components/StageChip";
import type { ContractBodies } from "./fleet";
import { stateOf, STATES, type State } from "./state";

/** Where a node counts in the story's header. */
export type NodeKind = "need" | "flight" | "ready" | "done" | "backlog";

export const KIND_ORDER: NodeKind[] = ["done", "flight", "need", "ready", "backlog"];

const KIND: Record<State, NodeKind> = {
  failed: "need",
  question: "need",
  paused: "need",
  needs_you: "need",
  stale: "need",
  ready_to_merge: "need",
  running: "flight",
  on_ci: "flight",
  parked: "flight",
  queued: "ready",
  held: "ready",
  todo: "ready",
  merged: "done",
  abandoned: "backlog",
  backlog: "backlog",
};

/** One child of a story as the graph draws it. */
export interface StoryNode {
  ticket: string;
  state: State;
  word: string;
  kind: NodeKind;
}

function mergedWord(mergedMs: number | null, now: number): string {
  if (mergedMs == null) return "merged";
  const today = localMidnight(now);
  if (mergedMs >= today) return `merged ${formatClock(mergedMs)}`;
  if (mergedMs >= localMidnight(today - 1)) return "merged yesterday";
  return `merged ${new Date(mergedMs).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`;
}

type OpenStatus = Exclude<StoryChildRow["status"], "merged" | "abandoned">;

/** Each drawn child's state and word: a merge, else its live run on the
 *  host's `/status`, else its `/attention` item, else its board facts. */
export function storyNodes(story: StoryDetail, host: HostRecord<ContractBodies>, now: number, readiness: ReadinessMap): StoryNode[] {
  const children = story.children.filter((child) => child.status !== "abandoned");
  const drawn = new Map(children.map((child) => [child.ticket, child]));
  const status = host.contract_error ? undefined : host.status;
  const runs = new Map((status?.runs ?? []).map((run) => [run.ticket, run]));
  const staleMs = status?.thresholds.heartbeat_stale_ms ?? Infinity;
  const items = hostItems(host, now);
  const readyOf = (run: unknown) => (typeof run === "number" ? readiness.get(runKey(host.key, run)) : undefined);

  const placed = (child: StoryChildRow): { state: State; word: string } => {
    if (child.status === "merged") return { state: "merged", word: mergedWord(child.merged_ms, now) };
    const run = runs.get(child.ticket);
    if (run != null) {
      const state = stateOf({ run, staleMs, readiness: readyOf(run.id) });
      const word = state === "running" ? stageWord({ phase: run.phase, round: run.round + 1, max_rounds: run.max_rounds }) : STATES[state].word;
      return { state, word };
    }
    const item = items.find((each) => each.ticket === child.ticket);
    if (item != null) {
      const state = stateOf({ attention: item, readiness: readyOf(item.run) });
      return { state, word: STATES[state].word };
    }
    if (child.column === "backlog") return { state: "backlog", word: STATES.backlog.word };
    const waiting = child.depends_on.filter((key) => drawn.has(key) && drawn.get(key)?.status !== "merged");
    if (waiting.length > 0) return { state: "todo", word: `waiting on ${waiting.join(", ")}` };
    if (child.status === "ready") return { state: "queued", word: STATES.queued.word };
    const state = stateOf({ board: child.status as OpenStatus });
    return { state, word: STATES[state].word };
  };

  return children.map((child) => {
    const { state, word } = placed(child);
    return { ticket: child.ticket, state, word, kind: KIND[state] };
  });
}

/** The story's own state: needs you before in flight before ready; done
 *  only once every node merged. */
export function storyState(nodes: StoryNode[]): { state: State; word: string } {
  const has = (kind: NodeKind) => nodes.some((node) => node.kind === kind);
  if (has("need")) return { state: "needs_you", word: "Needs you" };
  if (has("flight")) return { state: "running", word: "In flight" };
  if (has("ready")) return { state: "queued", word: "Ready" };
  if (nodes.length > 0 && nodes.every((node) => node.kind === "done")) return { state: "merged", word: "Done" };
  return { state: "backlog", word: "Backlog" };
}
