import { expect, test } from "bun:test";
import { collapseFailed, groupFailed } from "../src/lib/attention";
import type { AttentionItem } from "../src/lib/types";

const failed = (ticket: string, run: number, reason: string): AttentionItem => ({
  kind: "failed",
  level: "attention",
  daemon: "writer:7710",
  ticket,
  run,
  reason,
  attempt: 1,
  ended_ms: 1756892800000 + run * 1000,
});

// Three strikes of KO-343 arrive newest first, with another ticket's one
// failure between them, the way `/attention` can interleave them.
const items: AttentionItem[] = [
  failed("KO-343", 176, "terminal adjudication: FAIL; branch task/ko-343 preserved at 046d7d70f5e1"),
  failed("KO-229", 88, "verify failed: 2 tests errored in test_store_surface after the rename"),
  failed("KO-343", 172, "verify failed before merge; branch task/ko-343 preserved at 7f2e1a0b9c8d"),
  failed("KO-343", 174, "implementer made no commits; nothing to review"),
];

test("three failed items of one ticket and one of another group into two entries, runs oldest first, the latest run's reason on top", () => {
  const groups = groupFailed(items);
  expect(groups.length).toBe(2);
  const [threes, one] = groups;
  expect(threes!.ticket).toBe("KO-343");
  expect(threes!.attempts.map((run) => run.run)).toEqual([172, 174, 176]);
  expect(threes!.latest.run).toBe(176);
  expect(threes!.latest.reason).toBe("terminal adjudication: FAIL; branch task/ko-343 preserved at 046d7d70f5e1");
  expect(one!.ticket).toBe("KO-229");
  expect(one!.attempts.length).toBe(1);
  expect(one!.latest.run).toBe(88);
});

test("the same ticket on two daemons is two groups; other kinds are not grouped", () => {
  const elsewhere = { ...failed("KO-343", 9, "verify failed: x"), daemon: "operator:7710" };
  const question: AttentionItem = { kind: "blocked", level: "attention", daemon: "writer:7710", ticket: "KO-343", question: "?" };
  const groups = groupFailed([question, ...items, elsewhere]);
  expect(groups.map((group) => [group.daemon, group.ticket, group.attempts.length])).toEqual([
    ["writer:7710", "KO-343", 3],
    ["writer:7710", "KO-229", 1],
    ["operator:7710", "KO-343", 1],
  ]);
});

test("collapsing keeps every other item in place and carries attempts only for a ticket that failed more than once", () => {
  const question: AttentionItem = { kind: "blocked", level: "attention", daemon: "writer:7710", ticket: "KO-240", question: "?" };
  const entries = collapseFailed([items[0]!, question, ...items.slice(1)]);
  expect(entries.map((entry) => [entry.item.kind, entry.item.ticket, entry.item.run, entry.attempts?.length])).toEqual([
    ["failed", "KO-343", 176, 3],
    ["blocked", "KO-240", undefined, undefined],
    ["failed", "KO-229", 88, undefined],
  ]);
});
