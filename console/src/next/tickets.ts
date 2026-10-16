import { useContext } from "react";
import { useSearch } from "@tanstack/react-router";
import { needsYouEntries, type NeedsYouEntry } from "../lib/attention";
import type { BoardColumn } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { hostItems } from "../lib/hosts";
import { useBoards, useMergedToday, useReadiness } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";

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

export interface TicketBoard {
  project: string | undefined;
  /** Whether some scoped host has answered `/board` at least once. */
  answered: boolean;
  columns: Record<ColumnId, TicketCard[]>;
  total: number;
}

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
    const needs = new Map<string, NeedsYouEntry>();
    for (const entry of needsYouEntries(hostItems(host, now), readiness, now)) {
      const ticket = entry.item.ticket;
      if (typeof ticket === "string" && !needs.has(ticket)) needs.set(ticket, entry);
    }
    const name = host.project == null ? host.label : projectName(host.project);
    for (const column of board.columns) {
      for (const ticket of column.tickets) {
        const card: TicketCard = {
          id: cardId(host.base, ticket.ticket),
          key: ticket.ticket,
          title: ticket.title,
          project: name,
          state: column.state,
          need: needs.get(ticket.ticket) ?? null,
        };
        columns[boardColumn(card)].push(card);
      }
    }
  }
  for (const row of merged) {
    const card: TicketCard = {
      id: `${row.daemon ?? ""} run ${row.id}`, key: row.ticket, title: row.title ?? "", project: row.project, state: "merged", need: null,
    };
    columns[boardColumn(card)].push(card);
  }
  return {
    project,
    answered: boards.length > 0,
    columns,
    total: COLUMN_IDS.reduce((sum, id) => sum + columns[id].length, 0),
  };
}
