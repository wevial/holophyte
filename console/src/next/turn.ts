import { useContext } from "react";
import type { RunDetail, RunTurn, TranscriptEntry } from "../lib/contract.gen";
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
  | { kind: "turn"; base: string; detail: RunDetail; turns: RunTurn[]; turn: RunTurn };

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
  return { kind: "turn", base: host.base, detail, turns: turns.data.turns, turn: selected };
}

export type TranscriptRow =
  | { kind: "prompt" | "note" | "said" | "error"; offset: string; text: string; cut: boolean }
  | { kind: "tool"; offset: string; name: string; summary: string; input?: TranscriptEntry; output?: TranscriptEntry; failed: boolean; duration: number | null }
  | { kind: "end"; offset: string; text: string };

function transcriptOffset(at: number | null, start: number | null): string {
  if (at == null || start == null) return "—";
  const seconds = Math.floor((at - start) / 1000);
  const absolute = Math.abs(seconds);
  return `${seconds < 0 ? "-" : "+"}${Math.floor(absolute / 60)}:${String(absolute % 60).padStart(2, "0")}`;
}

function toolRow(input: TranscriptEntry | undefined, output: TranscriptEntry | undefined, offset: string): TranscriptRow {
  return {
    kind: "tool", offset,
    name: input?.tool ?? output?.tool ?? "Tool",
    summary: input?.summary ?? output?.summary ?? "",
    input, output,
    failed: output?.failed ?? false,
    duration: input?.at_ms != null && output?.at_ms != null ? (output.at_ms - input.at_ms) / 1000 : null,
  };
}

export function transcriptRows(entries: TranscriptEntry[], turn: RunTurn): TranscriptRow[] {
  const start = turn.seconds == null ? null : turn.ended_ms - turn.seconds * 1000;
  const commands = new Set(entries.filter((entry) => entry.speaker === "command" && entry.call != null).map((entry) => entry.call));
  const results = new Map(entries.filter((entry) => entry.speaker === "tool" && entry.call != null).map((entry) => [entry.call, entry]));
  const rows: TranscriptRow[] = [];
  const prompt = entries.find((entry) => entry.speaker === "user");
  const timed = entries.filter((entry) => entry.at_ms != null).sort((a, b) => a.at_ms! - b.at_ms!);
  let timedIndex = 0;
  for (const entry of entries.map((entry) => entry.at_ms == null ? entry : timed[timedIndex++]!)) {
    const offset = transcriptOffset(entry.at_ms, start);
    if (entry.speaker === "command") {
      rows.push(toolRow(entry, entry.call == null ? undefined : results.get(entry.call), offset));
    } else if (entry.speaker === "tool") {
      if (entry.call == null || !commands.has(entry.call)) rows.push(toolRow(undefined, entry, offset));
    } else {
      const kind = entry.speaker === "user" ? entry === prompt ? "prompt" : "note" : entry.speaker === "assistant" ? "said" : "error";
      rows.push({ kind, offset, text: entry.text, cut: entry.cut });
    }
  }
  const ending = turn.timed_out ? " · timed out" : turn.exit_status == null ? "" : ` · exit ${turn.exit_status}`;
  rows.push({ kind: "end", offset: transcriptOffset(turn.ended_ms, start), text: `Turn ended${ending}` });
  return rows;
}
