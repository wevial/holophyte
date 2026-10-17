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
  | { kind: "tool"; offset: string; name: string; summary: string; input?: TranscriptEntry; output?: TranscriptEntry; failed: boolean | null; duration: number | null }
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
    failed: output?.failed ?? null,
    duration: input?.at_ms != null && output?.at_ms != null ? (output.at_ms - input.at_ms) / 1000 : null,
  };
}

export function transcriptRows(entries: TranscriptEntry[], turn: RunTurn): TranscriptRow[] {
  const start = turn.seconds == null ? null : turn.ended_ms - turn.seconds * 1000;
  const resultQueues = new Map<string, TranscriptEntry[]>();
  for (const entry of entries) {
    if (entry.speaker !== "tool" || entry.call == null) continue;
    const queue = resultQueues.get(entry.call) ?? [];
    queue.push(entry);
    resultQueues.set(entry.call, queue);
  }
  const pairedResults = new Map<TranscriptEntry, TranscriptEntry>();
  const matchedResults = new Set<TranscriptEntry>();
  for (const entry of entries) {
    if (entry.speaker !== "command" || entry.call == null) continue;
    const result = resultQueues.get(entry.call)?.shift();
    if (result == null) continue;
    pairedResults.set(entry, result);
    matchedResults.add(result);
  }
  const rows: TranscriptRow[] = [];
  const prompt = entries.find((entry) => entry.speaker === "user");
  const timed = entries.filter((entry) => entry.at_ms != null).sort((a, b) => a.at_ms! - b.at_ms!);
  let timedIndex = 0;
  for (const entry of entries.map((entry) => entry.at_ms == null ? entry : timed[timedIndex++]!)) {
    const offset = transcriptOffset(entry.at_ms, start);
    if (entry.speaker === "command") {
      rows.push(toolRow(entry, pairedResults.get(entry), offset));
    } else if (entry.speaker === "tool") {
      if (!matchedResults.has(entry)) rows.push(toolRow(undefined, entry, offset));
    } else {
      const kind = entry.speaker === "user" ? entry === prompt ? "prompt" : "note" : entry.speaker === "assistant" ? "said" : "error";
      rows.push({ kind, offset, text: entry.text, cut: entry.cut });
    }
  }
  const ending = turn.timed_out ? " · timed out" : turn.exit_status == null ? "" : ` · exit ${turn.exit_status}`;
  rows.push({ kind: "end", offset: transcriptOffset(turn.ended_ms, start), text: `Turn ended${ending}` });
  return rows;
}

export type TurnFilter = "All" | "Said" | "Tools" | "Errors";

export function failedRow(row: TranscriptRow): boolean {
  return row.kind === "error" || (row.kind === "tool" && row.failed === true);
}

export function matchParts(text: string, query: string): { text: string; match: boolean }[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [{ text, match: false }];
  const lower = text.toLowerCase();
  const parts = [];
  let start = 0;
  let index = lower.indexOf(needle);
  while (index !== -1) {
    if (index > start) parts.push({ text: text.slice(start, index), match: false });
    parts.push({ text: text.slice(index, index + needle.length), match: true });
    start = index + needle.length;
    index = lower.indexOf(needle, start);
  }
  if (start < text.length) parts.push({ text: text.slice(start), match: false });
  return parts;
}

export function searchTranscript(rows: TranscriptRow[], query: string, filter: TurnFilter) {
  const counts = { All: rows.length, Said: 0, Tools: 0, Errors: 0 };
  const entries = rows.map((row, index) => {
    const said = row.kind === "said" || row.kind === "note";
    const tool = row.kind === "tool" || row.kind === "prompt";
    const failed = failedRow(row);
    counts.Said += Number(said);
    counts.Tools += Number(tool);
    counts.Errors += Number(failed);
    const texts = row.kind === "tool" ? [row.summary, row.input?.text ?? "", row.output?.text ?? ""] : [row.text];
    const matches = texts.reduce((total, text) => total + matchParts(text, query).filter(part => part.match).length, 0);
    const passes = filter === "All" || (filter === "Said" && said) || (filter === "Tools" && tool) || (filter === "Errors" && failed);
    return { row, index, matches, visible: passes && (!query.trim() || matches > 0) };
  });
  return { counts, entries, matches: entries.reduce((total, entry) => total + entry.matches, 0), matchingEntries: entries.filter(entry => entry.matches > 0).length };
}
