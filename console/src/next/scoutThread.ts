import { ACTIONS_OFF } from "../lib/actions";
import type { ProposalBlock, ProposalChoice, ScoutDetail, ScoutEvent, ThreadMessage } from "../lib/contract.gen";
import { formatClock, formatSettled, formatSpan } from "../lib/format";
import { isLive, numberFindings } from "./scout";

export type LineTone = "quiet" | "bad" | "wait";

export type ThreadRow =
  | { kind: "message"; key: string; head: string; at: number; text: string; agent: boolean }
  | { kind: "line"; key: string; text: string; tone: LineTone }
  | { kind: "card"; key: string; id: number; at: number; question: string; choices: ProposalChoice[]; answered: boolean };

export type CardRow = Extract<ThreadRow, { kind: "card" }>;

const ANSWER_ENDS = new Set(["scout_answered", "scout_answer_failed", "scout_answer_stopped"]);
const FAILED = new Set(["scout_answer_failed", "scout_pass_failed"]);
const STOPPED = new Set(["scout_answer_stopped", "scout_pass_stopped"]);
const PASS_ENDS = new Set(["scout_pass_done", "scout_pass_failed", "scout_pass_stopped"]);

const asksOrSteers = (message: ThreadMessage) => message.mode === "ask" || message.mode === "steer";

/** "You · ask", "Scout", or "test seat via MCP · steer". */
function messageHead(message: ThreadMessage): string {
  if (message.mode === "answer") return message.author === "scout" ? "Scout" : message.author;
  return `${message.author === "maintainer" ? "You" : message.author} · ${message.mode}`;
}

function messageRow(detail: ScoutDetail, message: ThreadMessage, index: number): ThreadRow {
  const proposal = message.author === "scout" ? message.blocks.find((block): block is ProposalBlock => block.type === "proposal") : undefined;
  if (proposal == null) {
    return { kind: "message", key: `message-${index}`, head: messageHead(message), at: message.at, text: message.text, agent: message.mode === "answer" };
  }
  return {
    kind: "card", key: `message-${index}`, id: message.id, at: message.at, question: proposal.question, choices: proposal.choices,
    answered: detail.thread.some((later) => later.answers === message.id),
  };
}

const highestPass = (detail: ScoutDetail, before = Infinity) =>
  Math.max(0, ...detail.reports.filter((report) => report.at < before).map((report) => report.pass));

export interface FollowUp {
  kind: "answer" | "pass";
  started: ScoutEvent;
  /** The first end event of its kind after the start, before the next start of its kind. */
  end: ScoutEvent | null;
  /** The next start of its kind, or null for the newest. */
  until: number | null;
  /** For a pass, one above the reports written before its start. */
  pass: number | null;
}

/** Each answer turn and follow-up pass of the scout, in the order of their start events. */
export function followUps(detail: ScoutDetail): FollowUp[] {
  const events = [...detail.events].sort((a, b) => a.at - b.at || a.id - b.id);
  return events.flatMap((started, index): FollowUp[] => {
    const kind = started.kind === "scout_answer_started" ? "answer" : started.kind === "scout_pass_started" ? "pass" : null;
    if (kind == null) return [];
    const ends = kind === "answer" ? ANSWER_ENDS : PASS_ENDS;
    const later = events.slice(index + 1);
    const next = later.findIndex((event) => event.kind === started.kind);
    const end = (next < 0 ? later : later.slice(0, next)).find((event) => ends.has(event.kind)) ?? null;
    return [{ kind, started, end, until: next < 0 ? null : later[next]!.at, pass: kind === "pass" ? highestPass(detail, started.at) + 1 : null }];
  });
}

/** The pass the scout is in: its newest pass, with no end, while the scout runs. */
export function livePass(detail: ScoutDetail): FollowUp | null {
  const newest = followUps(detail).findLast((each) => each.kind === "pass");
  return isLive(detail.scout.phase) && newest != null && newest.end == null ? newest : null;
}

const spanOf = (events: FollowUp[], started: ScoutEvent) => events.find((each) => each.started === started)!;

function answerLine(detail: ScoutDetail, turn: FollowUp): string {
  const span = turn.end != null ? formatSpan(turn.end.at - turn.started.at) : detail.scout.phase === "answering" && turn.until == null ? "now" : null;
  return ["answer turn", detail.scout.lead, span].filter((part) => part != null).join(" · ");
}

function passLine(detail: ScoutDetail, run: FollowUp): string {
  const { started, pass } = run;
  const helpers = detail.tasks.filter((task) => task.pass === pass && task.started_ms != null && task.started_ms >= started.at && task.started_ms < (run.until ?? Infinity)).length;
  const route = helpers ? [detail.scout.helper, `×${helpers}`].filter((part) => part != null).join(" ") : null;
  return [`pass ${pass}`, route, formatClock(started.at)].filter((part) => part != null).join(" · ");
}

function passDoneLine(detail: ScoutDetail, spans: FollowUp[], done: ScoutEvent): string {
  const run = spans.find((each) => each.end === done);
  const pass = run?.pass ?? highestPass(detail, done.at + 1);
  const labels = [
    ...numberFindings(detail.reports).filter((each) => each.pass === pass).map((each) => each.id),
    ...detail.actions.filter((action) => action.pass === pass).sort((a, b) => a.position - b.position).map((action) => `A${action.position}`),
  ];
  return [
    `pass ${pass} done`,
    run == null ? null : formatSettled(done.at - run.started.at),
    labels.length ? labels.join(", ") : null,
    formatClock(done.at),
  ].filter((part) => part != null).join(" · ");
}

function eventLine(detail: ScoutDetail, events: ScoutEvent[], spans: FollowUp[], index: number): { text: string; tone: LineTone } | null {
  const event = events[index]!;
  if (event.kind === "scout_answer_started") return { text: answerLine(detail, spanOf(spans, event)), tone: "quiet" };
  if (event.kind === "scout_pass_started") return { text: passLine(detail, spanOf(spans, event)), tone: "quiet" };
  if (event.kind === "scout_pass_done") return { text: passDoneLine(detail, spans, event), tone: "quiet" };
  if (FAILED.has(event.kind)) return { text: event.summary, tone: "bad" };
  if (STOPPED.has(event.kind)) return { text: event.summary, tone: "wait" };
  if (event.kind === "scout_steer_taken") return { text: `${event.summary} · ${formatClock(event.at)}`, tone: "quiet" };
  return null;
}

/** What an ask or steer no turn has taken yet waits for, or null on a scout that will take none. */
function waitingFor(detail: ScoutDetail): string | null {
  const { phase } = detail.scout;
  if (phase === "failed" || phase === "stopped") return null;
  return isLive(phase) ? "the next wave or the end" : "a scout slot";
}

/** The scout's messages and the lines its answer turns and passes draw, in
 *  time order, a message before an event at the same moment; an ask or steer
 *  no turn has taken yet is followed by its waiting line, and the scout's
 *  proposal reads as a card, answered once a message names it. */
export function threadRows(detail: ScoutDetail): ThreadRow[] {
  const events = [...detail.events].sort((a, b) => a.at - b.at || a.id - b.id);
  const waiting = waitingFor(detail);
  const timed: { at: number; order: number; rows: ThreadRow[] }[] = detail.thread.map((message, index) => ({
    at: message.at,
    order: 0,
    rows: [
      messageRow(detail, message, index),
      ...(waiting != null && asksOrSteers(message) && message.consumed_by == null
        ? [{ kind: "line", key: `waiting-${index}`, text: `waiting for ${waiting}`, tone: "wait" } as const]
        : []),
    ],
  }));
  const spans = followUps(detail);
  events.forEach((event, index) => {
    const line = eventLine(detail, events, spans, index);
    if (line != null) timed.push({ at: event.at, order: 1, rows: [{ kind: "line", key: `event-${index}`, ...line }] });
  });
  return timed.sort((a, b) => a.at - b.at || a.order - b.order).flatMap((each) => each.rows);
}

/** Whether the composer sends to a scout in `phase`, its actions and waiting messages aside. */
export const takesMessages = (phase: ScoutDetail["scout"]["phase"]) => phase === "done" || isLive(phase);

export const hasThread = (detail: ScoutDetail) =>
  takesMessages(detail.scout.phase) || detail.scout.phase === "answering" || detail.thread.length > 0;

/** Why the composer cannot send now, or null when it can: the daemon takes a
 *  message on a done or live scout with no ask or steer waiting. */
export function composeBlock(detail: ScoutDetail, actions: boolean): string | null {
  const { scout } = detail;
  if (!actions) return ACTIONS_OFF;
  if (!takesMessages(scout.phase)) return `Scout ${scout.id} is ${scout.phase}.`;
  const waiting = detail.thread.find((message) => asksOrSteers(message) && message.consumed_by == null);
  return waiting == null ? null : `Message ${waiting.id} waits for ${waitingFor(detail)}.`;
}

/** What the composer's line says a message in `mode` does: on a live scout an
 *  ask waits for its end and a steer for its next wave, or starts the pass after the running one. */
export function composeLine(detail: ScoutDetail, mode: ThreadMessage["mode"]): string {
  if (!isLive(detail.scout.phase)) return mode === "ask" ? "Answers from the report." : `Starts pass ${highestPass(detail) + 1}.`;
  return mode === "ask" ? "Answered when the scout is done." : `Taken at the next wave, or as pass ${(livePass(detail)?.pass ?? 1) + 1}.`;
}
