import { useContext } from "react";
import { useQueries } from "@tanstack/react-query";
import type { RouteLabels, RunDetail, TicketDetail } from "../lib/contract.gen";
import type { HostRecord } from "../lib/hosts";
import { useBoards, useReadiness, useRun, ticketQuery } from "./queries";
import type { ContractBodies } from "./fleet";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import type { State } from "./state";
import { boardColumn, cardWord, COLUMN_HEADS, hostCards, type TicketCard } from "./tickets";

/** The board line's chip: the column's glyph and word, then what is
 *  happening to the ticket. */
export interface BoardChip {
  state: State;
  label: string;
  need: boolean;
  sub: string | null;
}

export type TicketPage =
  | { kind: "loading" }
  | { kind: "unknown_project" }
  | { kind: "not_mirrored" }
  | {
      kind: "ticket";
      host: HostRecord<ContractBodies>;
      ticket: TicketDetail;
      card: TicketCard | null;
      chip: BoardChip | null;
      liveRun: number | null;
      labels: RouteLabels | null;
      run: RunDetail | null;
      now: number;
    };

function cardChip(card: TicketCard, now: number): BoardChip {
  const column = boardColumn(card);
  const word = cardWord(card, now);
  return {
    state: COLUMN_HEADS[column].state,
    label: COLUMN_HEADS[column].label,
    need: column === "needs_you",
    sub: card.runId == null ? word : `run ${card.runId} · ${word}`,
  };
}

/** A ticket `/board` no longer lists reads its chip from its own status. */
function statusChip(ticket: TicketDetail): BoardChip | null {
  if (ticket.status === "merged") return { state: "merged", label: "Merged", need: false, sub: null };
  if (ticket.status !== "abandoned") return null;
  const label = ticket.current?.column === "canceled" ? "Cancelled" : "Abandoned";
  return { state: "backlog", label, need: false, sub: null };
}

/** `key` of `project`: the first of the project's hosts whose `/tickets/KEY`
 *  answers, with its board card, its `/status` and its run. */
export function useTicketPage(project: string, key: string): TicketPage {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { now } = peers;
  const hosts = scopedHosts(peers.hosts, project);
  const answers = useQueries({ queries: hosts.map((host) => ticketQuery(host.base, key, deps.fetch)) });
  const pending = answers.findIndex((answer) => answer.isPending);
  const found = (pending < 0 ? answers : answers.slice(0, pending)).findIndex((answer) => answer.data != null);
  const host = found < 0 ? null : hosts[found]!;
  const ticket = found < 0 ? null : answers[found]!.data!;
  const readiness = useReadiness(host == null ? [] : [host], deps);
  const boards = useBoards(host == null ? [] : [host], deps);
  const run = useRun(host?.base ?? null, ticket?.run ?? null, deps);
  if (host == null || ticket == null) {
    if (peers.polls > 0 && hosts.length === 0) return { kind: "unknown_project" };
    if (hosts.length > 0 && answers.every((answer) => answer.isSuccess && answer.data == null)) return { kind: "not_mirrored" };
    return { kind: "loading" };
  }
  const board = boards[0]?.board;
  const card = board == null ? null : (hostCards(host, board, readiness, now).find((each) => each.key === key) ?? null);
  const status = host.contract_error ? undefined : host.status;
  return {
    kind: "ticket",
    host,
    ticket,
    card,
    chip: card == null ? statusChip(ticket) : cardChip(card, now),
    liveRun: card?.run?.id ?? null,
    labels: status?.route_labels ?? null,
    run: run.data ?? null,
    now,
  };
}
