import { ACTIONS_OFF } from "../lib/actions";
import type { ProposalBlock, ProposalChoice, ScoutDetail, ScoutEvent, ThreadMessage } from "../lib/contract.gen";
import { formatClock, formatSettled, formatSpan } from "../lib/format";
import { numberFindings } from "./scout";

export type LineTone = "quiet" | "bad" | "wait";

export type ThreadRow =
  | { kind: "message"; key: string; head: string; at: number; text: string; agent: boolean }
  | { kind: "line"; key: string; text: string; tone: LineTone }
  | { kind: "card"; key: string; id: number; head: string; at: number; question: string; choices: ProposalChoice[]; answered: boolean };

export type CardRow = Extract<ThreadRow, { kind: "card" }>;

const ANSWER_ENDS = new Set(["scout_answered", "scout_answer_failed", "scout_answer_stopped"]);
const FAILED = new Set(["scout_answer_failed", "scout_pass_failed"]);
const STOPPED = new Set(["scout_answer_stopped", "scout_pass_stopped"]);

const asksOrSteers = (message: ThreadMessage) => message.mode === "ask" || message.mode === "steer";

/** "You · ask", "Scout", or "test seat via MCP · steer". */
function messageHead(message: ThreadMessage): string {
  if (message.mode === "answer") return message.author === "scout" ? "Scout" : message.author;
  return `${message.author === "maintainer" ? "You" : message.author} · ${message.mode}`;
}

const proposalOf = (message: ThreadMessage) =>
  message.author === "scout" ? message.blocks.find((block): block is ProposalBlock => block.type === "proposal") : undefined;

function messageRow(detail: ScoutDetail, message: ThreadMessage, index: number): ThreadRow {
  const proposal = proposalOf(message);
  if (proposal == null) {
    return { kind: "message", key: `message-${index}`, head: messageHead(message), at: message.at, text: message.text, agent: message.mode === "answer" };
  }
  return {
    kind: "card", key: `message-${index}`, id: message.id, head: "Scout", at: message.at, question: proposal.question, choices: proposal.choices,
    answered: detail.thread.some((later) => later.answers === message.id),
  };
}

const highestPass = (detail: ScoutDetail, before = Infinity) =>
  Math.max(0, ...detail.reports.filter((report) => report.at < before).map((report) => report.pass));

/** The pass a `scout_pass_started` event began: one above the reports written before it. */
const passOf = (detail: ScoutDetail, started: ScoutEvent) => highestPass(detail, started.at) + 1;

/** The pass a steer would start now. */
export const nextPass = (detail: ScoutDetail) => highestPass(detail) + 1;

function answerLine(detail: ScoutDetail, events: ScoutEvent[], index: number): string {
  const started = events[index]!;
  const later = events.slice(index + 1);
  const next = later.findIndex((event) => event.kind === "scout_answer_started");
  const end = (next < 0 ? later : later.slice(0, next)).find((event) => ANSWER_ENDS.has(event.kind));
  const span = end != null ? formatSpan(end.at - started.at) : detail.scout.phase === "answering" && next < 0 ? "now" : null;
  return ["answer turn", detail.scout.lead, span].filter((part) => part != null).join(" · ");
}

function passLine(detail: ScoutDetail, events: ScoutEvent[], index: number): string {
  const started = events[index]!;
  const pass = passOf(detail, started);
  const until = events.slice(index + 1).find((event) => event.kind === "scout_pass_started")?.at ?? Infinity;
  const helpers = detail.tasks.filter((task) => task.pass === pass && task.started_ms != null && task.started_ms >= started.at && task.started_ms < until).length;
  const route = helpers ? [detail.scout.helper, `×${helpers}`].filter((part) => part != null).join(" ") : null;
  return [`pass ${pass}`, route, formatClock(started.at)].filter((part) => part != null).join(" · ");
}

function passDoneLine(detail: ScoutDetail, events: ScoutEvent[], index: number): string {
  const done = events[index]!;
  const started = events.slice(0, index).findLast((event) => event.kind === "scout_pass_started");
  const pass = started != null ? passOf(detail, started) : highestPass(detail, done.at + 1);
  const labels = [
    ...numberFindings(detail.reports).filter((each) => each.pass === pass).map((each) => each.id),
    ...detail.actions.filter((action) => action.pass === pass).sort((a, b) => a.position - b.position).map((action) => `A${action.position}`),
  ];
  return [
    `pass ${pass} done`,
    started == null ? null : formatSettled(done.at - started.at),
    labels.length ? labels.join(", ") : null,
    formatClock(done.at),
  ].filter((part) => part != null).join(" · ");
}

function eventLine(detail: ScoutDetail, events: ScoutEvent[], index: number): { text: string; tone: LineTone } | null {
  const event = events[index]!;
  if (event.kind === "scout_answer_started") return { text: answerLine(detail, events, index), tone: "quiet" };
  if (event.kind === "scout_pass_started") return { text: passLine(detail, events, index), tone: "quiet" };
  if (event.kind === "scout_pass_done") return { text: passDoneLine(detail, events, index), tone: "quiet" };
  if (FAILED.has(event.kind)) return { text: event.summary, tone: "bad" };
  if (STOPPED.has(event.kind)) return { text: event.summary, tone: "wait" };
  return null;
}

/** The scout's messages and the lines its answer turns and passes draw, in
 *  time order, a message before an event at the same moment; an ask or steer
 *  no turn has taken yet is followed by its waiting line, and the scout's
 *  proposal reads as a card, answered once a message names it. */
export function threadRows(detail: ScoutDetail): ThreadRow[] {
  const events = [...detail.events].sort((a, b) => a.at - b.at || a.id - b.id);
  const timed: { at: number; order: number; rows: ThreadRow[] }[] = detail.thread.map((message, index) => ({
    at: message.at,
    order: 0,
    rows: [
      messageRow(detail, message, index),
      ...(asksOrSteers(message) && message.consumed_by == null
        ? [{ kind: "line", key: `waiting-${index}`, text: "waiting for a scout slot", tone: "wait" } as const]
        : []),
    ],
  }));
  events.forEach((event, index) => {
    const line = eventLine(detail, events, index);
    if (line != null) timed.push({ at: event.at, order: 1, rows: [{ kind: "line", key: `event-${index}`, ...line }] });
  });
  return timed.sort((a, b) => a.at - b.at || a.order - b.order).flatMap((each) => each.rows);
}

export const hasThread = (detail: ScoutDetail) => detail.scout.phase === "done" || detail.thread.length > 0;

/** Why the composer cannot send now, or null when it can: the daemon takes a
 *  message only on a done scout with no ask or steer waiting. */
export function composeBlock(detail: ScoutDetail, actions: boolean): string | null {
  const { scout } = detail;
  if (!actions) return ACTIONS_OFF;
  if (scout.phase !== "done") return `Scout ${scout.id} is ${scout.phase}.`;
  const waiting = detail.thread.find((message) => asksOrSteers(message) && message.consumed_by == null);
  return waiting == null ? null : `Message ${waiting.id} waits for a scout slot.`;
}
