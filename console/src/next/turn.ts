import { useContext } from "react";
import type { RunDetail, RunTurn } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { AnswerError } from "../lib/poll";
import { useRun, useRunTurns } from "./queries";
import { keepScope, PollDepsContext, scopedHosts, useConsolePeers } from "./scope";

const SEATS: Record<RunTurn["role"], string> = {
  implement: "implementer", review: "reviewer", adjudicate: "adjudicator", write: "writer", trim: "trim",
};

export function turnTitles(turns: RunTurn[]) {
  let implement = 0;
  let review = 0;
  return [...turns].sort((a, b) => a.id - b.id).map((turn) => {
    const seat = SEATS[turn.role];
    const ordinal = turn.role === "implement" ? `turn ${++implement}` : turn.role === "review" ? `round ${++review}` : "";
    return { turn, seat, ordinal, title: `${seat}${ordinal ? ` · ${ordinal}` : ""}` };
  });
}

export function turnPage(project: string, run: number, turn: number) {
  return { to: "/runs/$project/$run/turns/$turn", params: { project: projectName(project), run: String(run), turn: String(turn) }, search: keepScope } as const;
}

type TurnPage =
  | { kind: "loading" }
  | { kind: "unknown_project" }
  | { kind: "not_found" }
  | { kind: "turn_not_found" }
  | { kind: "turn"; detail: RunDetail; turns: RunTurn[]; turn: RunTurn };

export function useTurnPage(project: string, run: number | null, turn: number | null): TurnPage {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const host = scopedHosts(peers.hosts, project)[0] ?? null;
  const answer = useRun(host?.base ?? null, run, deps);
  const detail = answer.data;
  const turns = useRunTurns(host?.base ?? "", run ?? 0, host != null && run != null && detail != null, detail?.run.outcome == null, deps);
  if (host == null) return { kind: peers.polls > 0 ? "unknown_project" : "loading" };
  if (run == null || (answer.error instanceof AnswerError && answer.error.status === 404)) return { kind: "not_found" };
  if (detail == null) return { kind: "loading" };
  if (turn == null) return { kind: "turn_not_found" };
  if (turns.data == null) return { kind: "loading" };
  const selected = turns.data.turns.find((each) => each.id === turn);
  if (selected == null) return { kind: "turn_not_found" };
  return { kind: "turn", detail, turns: turns.data.turns, turn: selected };
}
