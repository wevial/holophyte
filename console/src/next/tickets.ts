import { useContext } from "react";
import { useSearch } from "@tanstack/react-router";
import { needsYouEntries, type NeedsYouEntry } from "../lib/attention";
import type { BoardColumn, StatusRun } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { formatAge, formatClock, formatDuration } from "../lib/format";
import { hostItems } from "../lib/hosts";
import { phaseLabel } from "../lib/runs";
import { prLabel } from "../lib/shipped";
import type { AttentionItem } from "../lib/types";
import { useBoards, useMergedToday, useReadiness } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { stateOf, STATES, type State } from "./state";

export type ColumnId = "backlog" | "ready" | "in_flight" | "needs_you" | "done";

export const COLUMN_IDS: ColumnId[] = ["backlog", "ready", "in_flight", "needs_you", "done"];

/** One ticket of one host; `state` is its `/board` state, or `merged` for
 *  a merge of today, which `/board` never lists. */
export interface TicketCard {
  id: string;
  key: string;
  title: string;
  project: string;
  state: BoardColumn["state"] | "merged";
  need: NeedsYouEntry | null;
  /** The ticket's pull request park while it is no Needs you entry. */
  park: AttentionItem | null;
  run: StatusRun | null;
  staleMs: number;
  queuedReason: string | null;
  held: boolean;
  timeBoxMs: number | null;
  waitsOn: string[];
  endedMs: number | null;
  /** The daemon's clock less the browser's, as of its last answer. */
  skewMs: number;
}

const BY_STATE: Record<TicketCard["state"], ColumnId> = {
  backlog: "backlog",
  needs_spec: "backlog",
  ready: "ready",
  blocked_on_deps: "ready",
  in_flight: "in_flight",
  blocked_on_operator: "in_flight",
  merged: "done",
};

export function boardColumn(card: Pick<TicketCard, "state" | "need">): ColumnId {
  return card.need != null && card.state !== "merged" ? "needs_you" : BY_STATE[card.state];
}

/** The Needs you entry's item, else a park, else the live run, else the board. */
export function cardState(card: TicketCard): State {
  if (card.state === "merged") return "merged";
  const item = card.need?.item ?? card.park;
  if (item != null) return stateOf({ attention: item, readiness: { ready: card.need?.readyToMerge === true } });
  if (card.run != null) return stateOf({ run: card.run, staleMs: card.staleMs });
  if (card.held && card.state === "ready") return "held";
  return stateOf({ board: card.state });
}

const firstLine = (text: unknown) => (typeof text === "string" && text.trim() !== "" ? text.split("\n")[0]! : null);
const num = (value: unknown) => (typeof value === "number" ? value : null);

/** The card's state line, after its glyph. */
export function cardWord(card: TicketCard, now: number): string {
  const state = cardState(card);
  const item = card.need?.item ?? card.park;
  const pr = typeof item?.pr_url === "string" ? ` · ${prLabel(item.pr_url)}` : "";
  switch (state) {
    case "running":
      if (card.run == null) return STATES.running.word;
      return card.run.phase === "reviewing" ? `${phaseLabel(card.run.phase)} · round ${card.run.round + 1} of ${card.run.max_rounds}` : phaseLabel(card.run.phase);
    case "on_ci":
    case "parked":
      return `${STATES[state].word}${pr}`;
    case "failed": {
      const reason = firstLine(item?.reason);
      return reason == null ? "failed" : `failed · ${reason}`;
    }
    case "question": {
      const asked = num(item?.asked_ms);
      return asked == null ? "question open" : `question open ${formatDuration(now + card.skewMs - asked)}`;
    }
    case "stale": {
      const beat = num(item?.heartbeat_age_ms) ?? card.run?.heartbeat_age_ms;
      return beat == null ? "stale" : `stale · no beat ${formatAge(beat)}`;
    }
    case "queued":
      return firstLine(card.queuedReason) ?? "queued";
    case "held":
      return `held with ${card.project}`;
    case "todo":
      if (card.state === "needs_spec") return "needs a spec";
      return card.waitsOn.length === 0 ? STATES.todo.word : `blocked by ${card.waitsOn.join(", ")}`;
    case "merged":
      return card.endedMs == null ? "merged" : `merged ${formatClock(card.endedMs)}`;
    default:
      return STATES[state].word;
  }
}

export interface TicketBoard {
  project: string | undefined;
  now: number;
  /** Whether some scoped host has answered `/board` at least once. */
  answered: boolean;
  columns: Record<ColumnId, TicketCard[]>;
  total: number;
}

const MINUTE_MS = 60_000;

const cardId = (base: string, key: string) => `${base} ${key}`;

/** The scoped fleet's tickets in the five columns of the Tickets page. */
export function useTicketBoard(): TicketBoard {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const { now } = peers;
  const hosts = scopedHosts(peers.hosts, project);
  const readiness = useReadiness(hosts, deps);
  const boards = useBoards(hosts, deps);
  const merged = useMergedToday(hosts, now, deps.fetch);
  const columns: Record<ColumnId, TicketCard[]> = { backlog: [], ready: [], in_flight: [], needs_you: [], done: [] };
  for (const { host, board } of boards) {
    const items = hostItems(host, now);
    const needs = new Map<string, NeedsYouEntry>();
    for (const entry of needsYouEntries(items, readiness, now)) {
      const ticket = entry.item.ticket;
      if (typeof ticket === "string" && !needs.has(ticket)) needs.set(ticket, entry);
    }
    const parks = new Map(items.filter((item) => item.kind === "pr_open").map((item) => [item.ticket, item]));
    const status = host.contract_error ? undefined : host.status;
    const runs = new Map((status?.runs ?? []).map((run) => [run.ticket, run]));
    const queued = new Map((status?.queued ?? []).map((entry) => [entry.ticket, entry.reason]));
    const name = host.project == null ? host.label : projectName(host.project);
    const skewMs = status == null ? 0 : status.now - (host.seen_ms ?? now);
    for (const column of board.columns) {
      for (const ticket of column.tickets) {
        const card: TicketCard = {
          id: cardId(host.base, ticket.ticket),
          key: ticket.ticket,
          title: ticket.title,
          project: name,
          state: column.state,
          need: needs.get(ticket.ticket) ?? null,
          park: parks.get(ticket.ticket) ?? null,
          run: runs.get(ticket.ticket) ?? null,
          staleMs: status?.thresholds.heartbeat_stale_ms ?? Infinity,
          queuedReason: queued.get(ticket.ticket) ?? null,
          held: status?.admission === "held",
          timeBoxMs: ticket.time_box_ms,
          waitsOn: ticket.waits_on,
          endedMs: null,
          skewMs,
        };
        columns[boardColumn(card)].push(card);
      }
    }
  }
  for (const row of merged) {
    const card: TicketCard = {
      id: `${row.daemon ?? ""} run ${row.id}`, key: row.ticket, title: row.title ?? "", project: row.project, state: "merged", need: null,
      park: null, run: null, staleMs: Infinity, queuedReason: null, held: false, timeBoxMs: row.estimate_min == null ? null : row.estimate_min * MINUTE_MS, waitsOn: [], endedMs: row.ended_ms, skewMs: 0,
    };
    columns[boardColumn(card)].push(card);
  }
  return {
    project,
    now,
    answered: boards.length > 0,
    columns,
    total: COLUMN_IDS.reduce((sum, id) => sum + columns[id].length, 0),
  };
}
