import type { Kind } from "../lib/attention";
import { isStale } from "../lib/derive";
import type { BoardState } from "../lib/types";

export type State =
  | "failed" | "question" | "paused" | "needs_you" | "stale" | "running" | "ready_to_merge"
  | "merged" | "queued" | "on_ci" | "parked" | "held" | "todo" | "backlog";

/** HOLO-275's raw colour tokens, by name. */
export type Tone = "bad" | "ask" | "need" | "live" | "wait" | "ink-2" | "ink-3";

export const STATES: Record<State, { word: string; tone: Tone }> = {
  failed: { word: "failed", tone: "bad" },
  question: { word: "question", tone: "ask" },
  paused: { word: "paused", tone: "ask" },
  needs_you: { word: "needs you", tone: "ask" },
  stale: { word: "stale", tone: "need" },
  running: { word: "running", tone: "live" },
  ready_to_merge: { word: "ready to merge", tone: "live" },
  merged: { word: "merged", tone: "live" },
  queued: { word: "queued", tone: "wait" },
  on_ci: { word: "on CI", tone: "wait" },
  parked: { word: "parked", tone: "wait" },
  held: { word: "held", tone: "wait" },
  todo: { word: "to do", tone: "ink-2" },
  backlog: { word: "backlog", tone: "ink-3" },
};

/** The caller fetches readiness (`GET /runs/N/merge`); none reads as not ready. */
type Readiness = { ready: boolean } | null | undefined;

type ParkFacts = { park_kind?: unknown };

export type Subject =
  | { attention: { kind: string } & ParkFacts; readiness?: Readiness }
  | {
      run: { phase: string; outcome?: string | null; heartbeat_age_ms?: number | null } & ParkFacts;
      staleMs: number;
      readiness?: Readiness;
    }
  | { board: BoardState };

const PARKED_ON_PR = "parked on its pull request";

const ATTENTION: Record<Kind, State | typeof PARKED_ON_PR> = {
  blocked: "question",
  pr_open: PARKED_ON_PR,
  paused: "paused",
  stale_run: "stale",
  failed: "failed",
  stuck_wait: "needs_you",
  supervisor: "needs_you",
  unreachable: "stale",
};

const BOARD: Record<BoardState, State> = {
  backlog: "backlog",
  needs_spec: "todo",
  blocked_on_deps: "todo",
  ready: "queued",
  blocked_on_operator: "needs_you",
  in_flight: "running",
};

const ENDED_PHASES = new Set(["done", "failed", "killed", "rejected", "paused"]);

function parked(facts: ParkFacts, readiness: Readiness): State {
  if (facts.park_kind === "ci") return "on_ci";
  return facts.park_kind === "pull_request" && readiness?.ready === true ? "ready_to_merge" : "parked";
}

function ofAttention(item: { kind: string } & ParkFacts, readiness: Readiness): State {
  const state = Object.hasOwn(ATTENTION, item.kind) ? ATTENTION[item.kind as Kind] : "needs_you";
  return state === PARKED_ON_PR ? parked(item, readiness) : state;
}

/** Every phase not named here is live, so a phase the daemon adds later reads running. */
function ofRun(run: Extract<Subject, { run: unknown }>["run"], staleMs: number, readiness: Readiness): State {
  if (run.phase === "awaiting_merge_approval") return parked(run, readiness);
  if (run.phase === "blocked_on_operator") return "question";
  if (ENDED_PHASES.has(run.phase)) {
    if (run.outcome === "merged") return "merged";
    return run.outcome === "paused" ? "paused" : "failed";
  }
  return isStale(run.heartbeat_age_ms, staleMs) ? "stale" : "running";
}

/** `held` has no subject: a lane draws it from the daemon's `admission`. */
export function stateOf(subject: Subject): State {
  if ("attention" in subject) return ofAttention(subject.attention, subject.readiness);
  if ("run" in subject) return ofRun(subject.run, subject.staleMs, subject.readiness);
  return BOARD[subject.board];
}

export type CardLook = "merged" | "live" | "todo" | "backlog";

export function cardOf(state: State): CardLook {
  if (state === "merged" || state === "backlog") return state;
  return state === "running" || state === "ready_to_merge" ? "live" : "todo";
}
