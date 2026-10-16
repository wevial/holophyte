import type { ReadinessMap } from "../lib/attention";
import type { StoryChildRow, StoryDetail } from "../lib/contract.gen";
import { formatClock } from "../lib/format";
import { hostItems, sinceSeen, type HostRecord } from "../lib/hosts";
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

/** One child of a story as the graph draws it; `depends_on` keeps only
 *  edges to the story's other drawn children. */
export interface StoryNode {
  ticket: string;
  title: string;
  run: number | null;
  time_box_ms: number | null;
  state: State;
  word: string;
  kind: NodeKind;
  depends_on: string[];
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
  const unseenMs = sinceSeen(host.seen_ms, now);
  const items = hostItems(host, now);
  const readyOf = (run: unknown) => (typeof run === "number" ? readiness.get(runKey(host.key, run)) : undefined);

  const placed = (child: StoryChildRow, depends: string[]): { state: State; word: string } => {
    if (child.status === "merged") return { state: "merged", word: mergedWord(child.merged_ms, now) };
    const run = runs.get(child.ticket);
    if (run != null) {
      const state = stateOf({ run: { ...run, heartbeat_age_ms: run.heartbeat_age_ms + unseenMs }, staleMs, readiness: readyOf(run.id) });
      const word = state === "running" ? stageWord({ phase: run.phase, round: run.round + 1, max_rounds: run.max_rounds }) : STATES[state].word;
      return { state, word };
    }
    const item = items.find((each) => each.ticket === child.ticket);
    if (item != null) {
      const state = stateOf({ attention: item, readiness: readyOf(item.run) });
      return { state, word: STATES[state].word };
    }
    if (child.column === "backlog") return { state: "backlog", word: STATES.backlog.word };
    const waiting = depends.filter((key) => drawn.get(key)?.status !== "merged");
    if (waiting.length > 0) return { state: "todo", word: `waiting on ${waiting.join(", ")}` };
    if (child.status === "ready") return { state: "queued", word: STATES.queued.word };
    const state = stateOf({ board: child.status as OpenStatus });
    return { state, word: STATES[state].word };
  };

  return children.map((child) => {
    const depends = child.depends_on.filter((key) => drawn.has(key));
    const { state, word } = placed(child, depends);
    return { ticket: child.ticket, title: child.title, run: child.run, time_box_ms: child.time_box_ms, state, word, kind: KIND[state], depends_on: depends };
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

export const CARD_W = 212;
export const CARD_H = 108;
const COLUMN_GAP = 72;
const ROW_GAP = 16;

/** Columns by longest path from a node with no dependency; a node met
 *  again while its own depth is open counts as depth 0, so a cycle ends. */
export function layoutGraph(nodes: StoryNode[]) {
  const byKey = new Map(nodes.map((node) => [node.ticket, node]));
  const depths = new Map<string, number>();
  const open = new Set<string>();
  const depthOf = (key: string): number => {
    const known = depths.get(key);
    if (known != null) return known;
    if (open.has(key)) return 0;
    open.add(key);
    const deps = byKey.get(key)!.depends_on;
    const depth = deps.length === 0 ? 0 : 1 + Math.max(...deps.map(depthOf));
    open.delete(key);
    depths.set(key, depth);
    return depth;
  };
  const columns: StoryNode[][] = [];
  for (const node of nodes) (columns[depthOf(node.ticket)] ??= []).push(node);
  const filled = columns.filter((column) => column != null);
  const at = new Map<string, { x: number; y: number }>();
  filled.forEach((column, c) => column.forEach((node, r) => at.set(node.ticket, { x: c * (CARD_W + COLUMN_GAP), y: r * (CARD_H + ROW_GAP) })));
  const edges = nodes.flatMap((node) =>
    node.depends_on.map((dep) => {
      const a = at.get(dep)!;
      const b = at.get(node.ticket)!;
      const [x1, y1, x2, y2] = [a.x + CARD_W, a.y + CARD_H / 2, b.x - 2, b.y + CARD_H / 2];
      const mx = (x1 + x2) / 2;
      const path = `M${x1} ${y1}C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}M${x2 - 6} ${y2 - 4}L${x2} ${y2}L${x2 - 6} ${y2 + 4}`;
      return { from: dep, to: node.ticket, path, dim: byKey.get(dep)!.state === "merged" && node.state === "merged" };
    }),
  );
  const rows = Math.max(0, ...filled.map((column) => column.length));
  const span = (count: number, size: number, gap: number) => (count === 0 ? 0 : count * size + (count - 1) * gap);
  return { columns: filled, at, edges, width: span(filled.length, CARD_W, COLUMN_GAP), height: span(rows, CARD_H, ROW_GAP) };
}
