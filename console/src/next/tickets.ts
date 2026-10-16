import { useContext } from "react";
import { useSearch } from "@tanstack/react-router";
import { needsYouEntries } from "../lib/attention";
import type { BoardColumn } from "../lib/contract.gen";
import { hostItems } from "../lib/hosts";
import { useBoards, useMergedToday, useReadiness } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";

export type ColumnId = "backlog" | "ready" | "in_flight" | "needs_you" | "done";

export const COLUMN_IDS: ColumnId[] = ["backlog", "ready", "in_flight", "needs_you", "done"];

/** One ticket of one host's `/board`, or a merge of today, which `/board`
 *  never lists. */
export interface TicketCard {
  id: string;
  key: string;
  title: string;
}

const BY_STATE: Record<BoardColumn["state"], ColumnId> = {
  backlog: "backlog",
  needs_spec: "backlog",
  ready: "ready",
  blocked_on_deps: "ready",
  in_flight: "in_flight",
  blocked_on_operator: "in_flight",
};

export interface TicketBoard {
  project: string | undefined;
  /** Whether some scoped host has answered `/board` at least once. */
  answered: boolean;
  columns: Record<ColumnId, TicketCard[]>;
  total: number;
}

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
    const needs = new Set(needsYouEntries(hostItems(host, now), readiness, now).map((entry) => entry.item.ticket));
    for (const column of board.columns) {
      for (const ticket of column.tickets) {
        columns[needs.has(ticket.ticket) ? "needs_you" : BY_STATE[column.state]].push({ id: `${host.base} ${ticket.ticket}`, key: ticket.ticket, title: ticket.title });
      }
    }
  }
  for (const row of merged) columns.done.push({ id: `${row.daemon ?? ""} run ${row.id}`, key: row.ticket, title: row.title ?? "" });
  return {
    project,
    answered: boards.length > 0,
    columns,
    total: COLUMN_IDS.reduce((sum, id) => sum + columns[id].length, 0),
  };
}
