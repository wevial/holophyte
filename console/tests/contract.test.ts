import { expect, test } from "bun:test";
import {
  type Attention,
  attentionSchema,
  boardSchema,
  findingsSchema,
  hostAttentionSchema,
  ledgerSchema,
  hostStatusSchema,
  mergeReadinessSchema,
  runAsksSchema,
  runDetailSchema,
  shippedSchema,
  statusSchema,
  ticketDetailSchema,
} from "../src/lib/contract.gen";
import status from "../../tests/fixtures/serve/status.json";
import attention from "../../tests/fixtures/serve/attention.json";
import hostStatus from "../../tests/fixtures/serve/host-status.json";
import hostAttention from "../../tests/fixtures/serve/host-attention.json";
import runDetail from "../../tests/fixtures/serve/run-detail.json";
import runDetailUnestimated from "../../tests/fixtures/serve/run-detail-unestimated.json";
import runMergeReady from "../../tests/fixtures/serve/run-merge-ready.json";
import runMergeBypassable from "../../tests/fixtures/serve/run-merge-bypassable.json";
import pinnedAsks from "../../tests/fixtures/serve/run-asks.json";
import shipped from "../../tests/fixtures/serve/shipped.json";
import ledger from "../../tests/fixtures/serve/ledger.json";
import findings from "../../tests/fixtures/serve/findings.json";
import board from "../../tests/fixtures/serve/board.json";
import ticket from "../../tests/fixtures/serve/ticket.json";

// The pinned asks hold `NOW` where the daemon sends a time.
const time = (value: string | null) => (value === "NOW" ? 1750000000000 : value);
const runAsks = { ...pinnedAsks, asks: pinnedAsks.asks.map((ask) => ({ ...ask, asked_ms: time(ask.asked_ms), answered_ms: time(ask.answered_ms) })) };

const firstIssuePath = (result: { success: boolean; error?: { issues: { path: PropertyKey[] }[] } }) =>
  result.error?.issues[0]?.path.join(".");

test("each pinned daemon answer parses unchanged with its generated schema", () => {
  expect<unknown>(statusSchema.parse(status)).toEqual(status);
  expect<unknown>(attentionSchema.parse(attention)).toEqual(attention);
  expect<unknown>(hostStatusSchema.parse(hostStatus)).toEqual(hostStatus);
  expect<unknown>(hostAttentionSchema.parse(hostAttention)).toEqual(hostAttention);
});

test("each pinned run-route answer parses unchanged with its generated schema", () => {
  expect<unknown>(runDetailSchema.parse(runDetail)).toEqual(runDetail);
  expect<unknown>(runDetailSchema.parse(runDetailUnestimated)).toEqual(runDetailUnestimated);
  expect<unknown>(mergeReadinessSchema.parse(runMergeReady)).toEqual(runMergeReady);
  expect<unknown>(mergeReadinessSchema.parse(runMergeBypassable)).toEqual(runMergeBypassable);
  expect<unknown>(runAsksSchema.parse(runAsks)).toEqual(runAsks);
});

test("each pinned list-route answer parses unchanged with its generated schema", () => {
  expect<unknown>(shippedSchema.parse(shipped)).toEqual(shipped);
  expect<unknown>(ledgerSchema.parse(ledger)).toEqual(ledger);
  expect<unknown>(boardSchema.parse(board)).toEqual(board);
  expect<unknown>(ticketDetailSchema.parse(ticket)).toEqual(ticket);
});

test("a board column state outside the board's states fails at its path", () => {
  const shipping = { ...board, columns: [{ ...board.columns[0], state: "shipping" }, ...board.columns.slice(1)] };
  expect(firstIssuePath(boardSchema.safeParse(shipping))).toStartWith("columns.0.state");
});

test("the pinned findings answer parses unchanged; a dropped follow-up state fails at its path", () => {
  expect<unknown>(findingsSchema.parse(findings)).toEqual(findings);
  const dropped = { ...findings, follow_ups: [{ ...findings.follow_ups[0], state: "dropped" }, ...findings.follow_ups.slice(1)] };
  expect(firstIssuePath(findingsSchema.safeParse(dropped))).toStartWith("follow_ups.0.state");
});

test("a ticket claimed by an older build carries its claim-time snapshot and parses", () => {
  // The daemon answers this only for a run claimed before revisions were recorded; no pin holds one.
  const claimed = { revision: null, title: ticket.title, acceptance_criteria: ticket.acceptance_criteria, verification_commands: ticket.verification_commands };
  const older = { ...ticket, claimed };
  expect<unknown>(ticketDetailSchema.parse(older)).toEqual(older);
});

test("a run detail without rounds or with a numeric phase fails at its path; an extra run key passes", () => {
  const { rounds: _rounds, ...roundless } = runDetail;
  const phase = { ...runDetail, run: { ...runDetail.run, phase: 42 } };
  expect(firstIssuePath(runDetailSchema.safeParse(roundless))).toBe("rounds");
  expect(firstIssuePath(runDetailSchema.safeParse(phase))).toBe("run.phase");
  const newer = { ...runDetail, run: { ...runDetail.run, later_field: "x" } };
  expect<unknown>(runDetailSchema.parse(newer)).toEqual(newer);
});

test("a newer daemon's extra keys pass at the top level and inside a run", () => {
  const newer = { ...status, later_field: 1, runs: [{ ...status.runs[0], later_field: "x" }, ...status.runs.slice(1)] };
  expect<unknown>(statusSchema.parse(newer)).toEqual(newer);
});

test("a mistyped field, an unknown kind and a missing key fail at their paths", () => {
  const phase = { ...status, runs: [{ ...status.runs[0], phase: 42 }] };
  const kind = { ...attention, items: [{ ...attention.items[0], kind: "later_kind" }] };
  const { supervisor: _supervisor, ...unsupervised } = status;
  const failures = [statusSchema.safeParse(phase), attentionSchema.safeParse(kind), statusSchema.safeParse(unsupervised)];
  expect(firstIssuePath(failures[0])).toStartWith("runs.0.phase");
  expect(firstIssuePath(failures[1])).toStartWith("items.0");
  expect(firstIssuePath(failures[2])).toStartWith("supervisor");
});

test("an attention item's kind narrows it to its own fields", () => {
  const item: Attention["items"][number] = attentionSchema.parse(attention).items[0];
  if (item.kind !== "pr_open") throw new Error(`expected pr_open, got ${item.kind}`);
  const number: number | null = item.pr.number;
  // @ts-expect-error a pr_open item declares no question: an undeclared key reads as unknown
  const question: string | null = item.question;
  expect(question).toBeUndefined();
  expect(number).toBe(10);
});
