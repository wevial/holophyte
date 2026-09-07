import { projectName } from "./derive";
import { formatAge, formatDuration } from "./format";
import type { HostRecord } from "./hosts";
import type { BoardBody, BoardState, Run } from "./types";

/** The five columns, left to right the path to merge. */
export const BOARD_STATES: BoardState[] = ["needs_spec", "blocked_on_deps", "ready", "blocked_on_operator", "in_flight"];

/** What the column header calls each state. */
export const STATE_LABELS: Record<BoardState, string> = {
  needs_spec: "needs_spec",
  blocked_on_deps: "blocked_on_deps",
  ready: "ready",
  blocked_on_operator: "blocked",
  in_flight: "in progress",
};

/** One card: a `/board` ticket joined with what its daemon already holds
 *  (the live run from `/status`, the question's `asked_ms` from
 *  `/attention`) and stamped with the daemon's project. */
export interface BoardCard {
  /** The daemon's base and the ticket: the same ticket on two daemons is two cards. */
  key: string;
  ticket: string;
  title: string | null;
  status: BoardState;
  project: string;
  run: Run | null;
  /** The live run's id as `/board` names it, kept when `/status` has no such run yet. */
  runId: number | null;
  /** The daemon's strike cap, for the strike pill. */
  strikesMax: number;
  question: string | null;
  waitsOn: string[];
  askedMs: number | null;
  /** The daemon's clock when it answered `/board`. */
  now: number;
}

const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** One host's `/board` as cards, in the wire's order. */
export function cardsOf(host: Pick<HostRecord, "base" | "project" | "status" | "attention">, body: BoardBody): BoardCard[] {
  const project = host.project == null ? "" : projectName(host.project);
  const runs = host.status?.runs ?? [];
  const strikesMax = host.status?.thresholds.strikes ?? 3;
  const cards: BoardCard[] = [];
  for (const column of body.columns) {
    for (const ticket of column.tickets) {
      const asked = host.attention?.items.find((item) => item.kind === "blocked" && item.ticket === ticket.ticket);
      cards.push({
        key: `${host.base}#${ticket.ticket}`,
        ticket: ticket.ticket,
        title: ticket.title,
        status: column.state,
        project,
        run: ticket.run == null ? null : (runs.find((candidate) => candidate.id === ticket.run) ?? null),
        runId: ticket.run,
        strikesMax,
        question: ticket.question,
        waitsOn: ticket.waits_on ?? [],
        askedMs: asked == null ? null : num(asked.asked_ms),
        now: body.now,
      });
    }
  }
  return cards;
}

export interface Column {
  state: BoardState;
  label: string;
  count: number;
  cards: BoardCard[];
}

/** The cards under the five columns in path order, every column present
 *  and counted; a card with a state the board does not know is dropped
 *  rather than misfiled. */
export function columns(cards: BoardCard[]): Column[] {
  return BOARD_STATES.map((state) => {
    const own = cards.filter((card) => card.status === state);
    return { state, label: STATE_LABELS[state], count: own.length, cards: own };
  });
}

/** The card's mono sub-line per state: what a needs_spec ticket lacks,
 *  what a dependent one waits on, how long a question has stood, or the
 *  live run's id, elapsed over box and heartbeat; a ready card has none.
 *  `now` is the daemon's clock the question is aged against. */
export function cardLine(card: BoardCard, now: number = card.now): string | null {
  switch (card.status) {
    case "needs_spec":
      return "no acceptance criteria yet";
    case "blocked_on_deps":
      return card.waitsOn.length === 0 ? null : card.waitsOn.map((id) => `waits on ${id}`).join(" · ");
    case "blocked_on_operator":
      return card.askedMs == null ? "question open" : `question open ${formatDuration(now - card.askedMs)}`;
    case "in_flight": {
      const { run } = card;
      if (run == null) return card.runId == null ? null : `#${card.runId}`;
      return `#${run.id} · ${formatDuration(run.elapsed_ms)} / ${formatAge(run.time_box_ms)} · hb ${formatAge(run.heartbeat_age_ms)}`;
    }
    default:
      return null;
  }
}
